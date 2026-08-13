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

@app.route('/api/account')
def get_account():
    # Placeholder account response
    return jsonify({
        "status": "success",
        "account": {
            "id": None,
            "name": "",
            "email": ""
        }
    })

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

if __name__ == '__main__':
    app.run(debug=True)

