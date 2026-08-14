import os
import pandas as pd
from dotenv import load_dotenv
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from requests.exceptions import RequestException
from supabase import create_client
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
        df = pd.read_csv(file)

        return jsonify({
            "status": "success",
            "message": "File uploaded successfully",
            "rows": len(df),
            "columns": list(df.columns)
        })

    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

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

