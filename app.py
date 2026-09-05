import os
import csv
from io import StringIO

import pandas as pd
from dotenv import load_dotenv
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from requests.exceptions import RequestException
from supabase import create_client
from werkzeug.utils import secure_filename
from crypto_service import CryptoProviderError, get_crypto_market_data
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
if SUPABASE_URL is None or SUPABASE_KEY is None:
    raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set in the environment")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# 1. Initialize the environment variable system
load_dotenv()

app = Flask(__name__)
CORS(app)

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_CSV_ROWS = 100_000
MAX_CSV_COLUMNS = 100
MAX_PREVIEW_ROWS = 100
ALLOWED_CSV_CONTENT_TYPES = {
    "",
    "application/csv",
    "application/vnd.ms-excel",
    "application/octet-stream",
    "text/csv",
    "text/plain",
}


class CSVValidationError(ValueError):
    """An upload is valid HTTP input but not a usable CSV dataset."""


def _json_safe(value):
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        return value.item()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def analyze_csv(file_storage):
    """Read a bounded UTF-8 CSV and return the analyst workspace payload."""
    original_name = file_storage.filename or ""
    safe_name = secure_filename(original_name)
    if not safe_name or "\x00" in original_name:
        raise CSVValidationError("Please choose a file with a valid filename.")
    if not safe_name.lower().endswith(".csv"):
        raise CSVValidationError("Only CSV files are supported.")
    if file_storage.content_type not in ALLOWED_CSV_CONTENT_TYPES:
        raise CSVValidationError("The uploaded file must be a CSV file.")

    raw_data = file_storage.stream.read(MAX_UPLOAD_BYTES + 1)
    if not raw_data:
        raise CSVValidationError("The CSV file is empty.")
    if len(raw_data) > MAX_UPLOAD_BYTES:
        raise CSVValidationError("The CSV file is too large. Maximum size is 10 MB.")
    try:
        csv_text = raw_data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise CSVValidationError("The CSV must use UTF-8 encoding.") from error
    if not csv_text.strip():
        raise CSVValidationError("The CSV file is empty.")

    try:
        csv_rows = list(csv.reader(StringIO(csv_text), strict=True))
    except csv.Error as error:
        raise CSVValidationError("The CSV is malformed.") from error
    if not csv_rows or not any(cell.strip() for cell in csv_rows[0]):
        raise CSVValidationError("The CSV must contain a header row.")
    header = csv_rows[0]
    if all(cell.strip().replace(".", "", 1).isdigit() for cell in header):
        raise CSVValidationError("The CSV must contain a header row.")
    expected_fields = len(header)
    if expected_fields == 0 or any(len(row) != expected_fields for row in csv_rows[1:]):
        raise CSVValidationError("The CSV is malformed: rows have inconsistent columns.")
    if len(csv_rows) - 1 > MAX_CSV_ROWS:
        raise CSVValidationError("The CSV contains too many rows. Maximum is 100,000.")

    try:
        dataframe = pd.read_csv(
            StringIO(csv_text),
            header=0,
            skip_blank_lines=True,
            on_bad_lines="error",
        )
    except (pd.errors.ParserError, pd.errors.EmptyDataError) as error:
        raise CSVValidationError(
            "The CSV is malformed or does not contain a header row."
        ) from error
    except ValueError as error:
        raise CSVValidationError("The CSV could not be parsed.") from error

    if dataframe.shape[1] == 0 or any(
        not isinstance(column, str) or not column.strip()
        for column in dataframe.columns
    ):
        raise CSVValidationError("The CSV must contain non-empty column headers.")
    if dataframe.shape[1] > MAX_CSV_COLUMNS:
        raise CSVValidationError("The CSV contains too many columns. Maximum is 100.")
    if len(dataframe) > MAX_CSV_ROWS:
        raise CSVValidationError("The CSV contains too many rows. Maximum is 100,000.")

    column_types = {}
    numeric_statistics = {}
    for column in dataframe.columns:
        series = dataframe[column]
        if pd.api.types.is_bool_dtype(series):
            detected_type = "boolean"
        elif pd.api.types.is_integer_dtype(series):
            detected_type = "integer"
        elif pd.api.types.is_float_dtype(series):
            detected_type = "number"
        elif pd.api.types.is_datetime64_any_dtype(series):
            detected_type = "datetime"
        else:
            detected_type = "text"
        column_types[column] = detected_type

        if pd.api.types.is_numeric_dtype(series):
            stats = series.describe()
            numeric_statistics[column] = {
                name: _json_safe(stats.get(name))
                for name in ("count", "mean", "std", "min", "25%", "50%", "75%", "max")
            }

    preview = [
        {column: _json_safe(value) for column, value in row.items()}
        for row in dataframe.head(MAX_PREVIEW_ROWS).to_dict(orient="records")
    ]
    return {
        "filename": safe_name,
        "rows": len(dataframe),
        "columns": int(dataframe.shape[1]),
        "column_names": [str(column) for column in dataframe.columns],
        "column_types": column_types,
        "missing_values": {
            str(column): int(count)
            for column, count in dataframe.isna().sum().items()
        },
        "duplicate_rows": int(dataframe.duplicated().sum()),
        "numeric_statistics": numeric_statistics,
        "preview": preview,
    }
@app.route('/register', methods=['POST'])
def register():
    try:
        data = request.get_json()

        email = data.get('email', '').strip()

        if not email:
            return jsonify({
                "status": "error",
                "message": "Email is required"
            }), 400

        response = supabase.auth.sign_up({
            "email": email
        })

        return jsonify({
            "status": "success",
            "message": "Registration received",
            "email": email
        })

    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

@app.route('/api/account')
def get_account():
    try:
        # Get the user's Supabase access token
        auth_header = request.headers.get("Authorization")

        if not auth_header or not auth_header.startswith("Bearer "):
            return jsonify({
                "status": "error",
                "message": "Authentication required"
            }), 401

        access_token = auth_header.split(" ", 1)[1]

        # Ask Supabase who this token belongs to
        user_response = supabase.auth.get_user(access_token)

        if not user_response or not user_response.user:
            return jsonify({
                "status": "error",
                "message": "Invalid authentication token"
            }), 401

        user = user_response.user
        user_id = user.id

        # Find this user's financial account
        account_response = (
            supabase
            .table("accounts")
            .select("id, user_id, fiat_balance_usd, btc_balance, eth_balance")
            .eq("user_id", user_id)
            .single()
            .execute()
        )

        account = account_response.data

        if not account:
            return jsonify({
                "status": "error",
                "message": "Account not found"
            }), 404

        return jsonify({
            "status": "success",
            "account": {
                "id": account["id"],
                "user_id": account["user_id"],
                "email": user.email,
                "fiat_balance_usd": account["fiat_balance_usd"],
                "btc_balance": account["btc_balance"],
                "eth_balance": account["eth_balance"]
            }
        })

    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

# 2. Home Route
@app.route('/')
def home():
    return send_from_directory('.', 'index.html')


# 3. Dashboard Route
@app.route('/dashboard.html')
def dashboard():
    return send_from_directory('.', 'dashboard.html')

@app.route('/platform.html')
def platform():
    return send_from_directory('.', 'platform.html')

@app.route('/login.html')
def login():
    return send_from_directory('.', 'login.html')

@app.route('/banking.html')
def banking():
    return send_from_directory('.', 'banking.html')

@app.route('/crypto.html')
def crypto():
    return send_from_directory('.', 'crypto.html')

@app.route('/crypto-portfolio.html')
def crypto_portfolio():
    return send_from_directory('.', 'crypto-portfolio.html')

@app.route('/customer-care.html')
def customer_care():
    return send_from_directory('.', 'customer-care.html')

@app.route('/support-console.html')
def support_console():
    return send_from_directory('.', 'support-console.html')

@app.route('/portfolio.html')
def portfolio():
    return send_from_directory('.', 'portfolio.html')

@app.route('/transactions.html')
def transactions():
    return send_from_directory('.', 'transactions.html')

@app.route('/accounts.html')
def accounts():
    return send_from_directory('.', 'accounts.html')

@app.route('/deposit.html')
def deposit():
    return send_from_directory('.', 'deposit.html')

@app.route('/withdrawal.html')
def withdrawal():
    return send_from_directory('.', 'withdrawal.html')

@app.route('/transfer.html')
def transfer():
    return send_from_directory('.', 'transfer.html')

@app.route('/ai-assistant.html')
def ai_assistant():
    return send_from_directory('.', 'ai-assistant.html')

@app.route('/market-research.html')
def market_research():
    return send_from_directory('.', 'market-research.html')

@app.route('/investment-research.html')
def investment_research():
    return send_from_directory('.', 'investment-research.html')


@app.route('/api/crypto', methods=['GET'])
def get_crypto():
    try:
        market_data = get_crypto_market_data()
    except CryptoProviderError as error:
        app.logger.warning("Crypto provider unavailable: %s", error)
        return jsonify({
            "status": "unavailable",
            "message": "Market data temporarily unavailable",
            "assets": [],
        }), 503

    return jsonify(market_data)

# 4. Upload Route
@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({
            "status": "error",
            "message": "No file uploaded"
        }), 400

    file = request.files['file']

    if file.filename == '':
        return jsonify({
            "status": "error",
            "message": "No file selected"
        }), 400

    try:
        analysis = analyze_csv(file)
    except CSVValidationError as error:
        return jsonify({
            "status": "error",
            "message": str(error)
        }), 400
    except (OSError, pd.errors.ParserError) as error:
        app.logger.warning("CSV processing failed: %s", error)
        return jsonify({
            "status": "error",
            "message": "The CSV could not be processed. Please check its contents."
        }), 400

    return jsonify({
        "status": "success",
        "message": "File uploaded and analyzed successfully",
        "analysis": analysis,
    })

# 5. research Route
@app.route('/research.html')
def research():
    return send_from_directory('.', 'research.html')
@app.route('/api/transactions', methods=['GET'])
def get_transactions():
    try:
        # 1. Get the authenticated user's Supabase access token
        auth_header = request.headers.get("Authorization")

        if not auth_header or not auth_header.startswith("Bearer "):
            return jsonify({
                "status": "error",
                "message": "Authentication required"
            }), 401

        access_token = auth_header.split(" ", 1)[1]

        # 2. Verify the token with Supabase
        user_response = supabase.auth.get_user(access_token)

        if not user_response or not user_response.user:
            return jsonify({
                "status": "error",
                "message": "Invalid authentication token"
            }), 401

        user_id = user_response.user.id

        # 3. Find the user's account
        account_response = (
            supabase
            .table("accounts")
            .select("id")
            .eq("user_id", user_id)
            .single()
            .execute()
        )

        account = account_response.data

        if not account:
            return jsonify({
                "status": "error",
                "message": "Account not found"
            }), 404

        account_id = account["id"]

        # 4. Retrieve transactions belonging to this account
        transactions_response = (
            supabase
            .table("transactions")
            .select("*")
            .eq("account_id", account_id)
            .order("created_at", desc=True)
            .execute()
        )

        return jsonify({
            "status": "success",
            "transactions": transactions_response.data or []
        })

    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500
if __name__ == '__main__':
    app.run(debug=True)

