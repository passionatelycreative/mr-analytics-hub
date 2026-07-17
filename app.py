from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from supabase import create_client, Client
from dotenv import load_dotenv    # Loads variables from your hidden .env file
import os

# 1. Initialize the environment variable system
load_dotenv()
app = Flask(__name__)
CORS(app)

# 1. The Home Route (loads index.html)
@app.route('/')
def home():
    return send_from_directory('.', 'index.html')

# 2. The Dashboard Route (loads dashboard.html)
@app.route('/dashboard.html')
def dashboard():
    return send_from_directory('.', 'dashboard.html')
# 2. Grab your credentials securely from the environment
SUPABASE_URL = os.environ.get('SUPABASE_URL','')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY','')

# 3. Establish the secure connection to your Supabase database cluster
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

@app.route('/submit-email', methods=['POST'])
def handle_submit():
    data = request.get_json()
    email = data.get('email')
    
    if not email:
        return jsonify({"status": "error", "message": "No email provided"}), 400

    try:
        # Push the researcher's email directly into your live cloud table
        supabase.table('registrations').insert({"email": email}).execute()
        
        print(f"[CLOUD SYNCED] Data safely injected into Supabase | Registered: {email}")
        return jsonify({"status": "success", "message": "Data tracked successfully"})
        
    except Exception as e:
        print(f"[DATABASE ERROR] Connection failed: {e}")
        
        # Handle the case where a researcher tries to register the exact same email twice
        if "duplicate key" in str(e).lower():
            return jsonify({"status": "success", "message": "Existing researcher verified"})
            
        return jsonify({"status": "error", "message": "Cloud transmission error"}), 500

if __name__ == '__main__':
    # Binds to the system's dynamic web port for clean deployment later
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)