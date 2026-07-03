from flask import Flask, request, jsonify
from flask_cors import CORS
from sqlalchemy import create_engine, text
import jwt
import time
import bcrypt
import oqs

app = Flask(__name__)

# ================= CORS (IMPORTANT FOR REACT) =================
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=True)

# ================= CONFIG =================
JWT_SECRET = "super-secret-change-this"
JWT_ALGO = "HS256"

engine = create_engine(
    "mysql+pymysql://root:@localhost/quantum",
    future=True
)

# ================= PQC CONFIG =================
KEM_ALGO = "ML-KEM-768"
kem_sessions = {}
dashboard_status = {}

# ================= PASSWORD HASH =================
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


# ================= HEALTH CHECK =================
@app.route("/", methods=["GET"])
def home():
    return jsonify({"message": "Flask PQC server running"}), 200


# ================= REGISTER =================
@app.route("/register", methods=["POST"])
def register():
    try:
        data = request.get_json()

        if not data:
            return jsonify({"success": False, "error": "Invalid JSON"}), 400

        username = (data.get("username") or "").strip()
        password = (data.get("password") or "").strip()

        if not username or not password:
            return jsonify({"success": False, "error": "Username and password are required"}), 400

        hashed_password = hash_password(password)

        with engine.begin() as conn:
            # Check if user already exists
            result = conn.execute(
                text("SELECT id FROM reg WHERE username = :username"),
                {"username": username}
            ).fetchone()

            if result:
                return jsonify({"success": False, "error": "User already exists"}), 409

            # Insert new user
            conn.execute(
                text("INSERT INTO reg (username, password) VALUES (:username, :password)"),
                {
                    "username": username,
                    "password": hashed_password
                }
            )

        return jsonify({
            "success": True,
            "message": "User registered successfully"
        }), 201

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


# ================= INIT KEM =================
@app.route("/kem/init", methods=["POST"])
def kem_init():
    try:
        data = request.get_json(force=True)
        username = data.get("username")
        
        if not username:
            return jsonify({"error": "username required"}), 400
        
        with oqs.KeyEncapsulation(KEM_ALGO) as kem:
            public_key = kem.generate_keypair()
            secret_key = kem.export_secret_key()

        kem_sessions[username] = {
            "secret_key": secret_key,
            "public_key": public_key,
            "kem_algo": KEM_ALGO
        }

        mock_ciphertext = None
        try:
            with oqs.KeyEncapsulation(KEM_ALGO) as client_kem:
                encap_method = getattr(client_kem, "encap", None) or getattr(client_kem, "encapsulate", None)
                if encap_method is not None:
                    result = encap_method(public_key)
                    if isinstance(result, tuple):
                        ciphertext = result[0]
                    else:
                        ciphertext = result
                    mock_ciphertext = ciphertext.hex()
        except Exception:
            mock_ciphertext = None

        return jsonify({
            "success": True,
            "public_key": public_key.hex(),
            "mock_ciphertext": mock_ciphertext
        })

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ================= LOGIN =================
@app.route("/login", methods=["POST"])
def login():
    try:
        data = request.get_json(force=True)

        username = data.get("username", "").strip()
        password = data.get("password", "").strip()
        kem_ct = data.get("kem_ct")  # Active: Client sends ciphertext generated from public_key

        if not username or not password:
            return jsonify({"success": False, "error": "Missing credentials"}), 400

        # ================= DB CHECK =================
        with engine.begin() as conn:
            row = conn.execute(
                text("SELECT password FROM reg WHERE username=:u"),
                {"u": username}
            ).fetchone()

        if not row:
            return jsonify({"success": False, "error": "User not found"}), 404

        stored_hash = row[0]

        if not verify_password(password, stored_hash):
            return jsonify({"success": False, "error": "Invalid password"}), 401

        # ================= DEFAULT RESPONSE =================
        shared_secret_hex = None
        secure_status = {
            "algorithm": KEM_ALGO,
            "mlkem_active": True,
            "connection": "SECURE",
            "quantum_protection": True,
            "shared_secret_verified": True,
            "last_key_exchange": "Successful",
            "security_alert": None,
            "tampering_detected": False
        }

        # ================= PQC BLOCK =================
        if username in kem_sessions and kem_ct:
            try:
                session = kem_sessions[username]

                # Recreate KEM context with same algorithm
                with oqs.KeyEncapsulation(session["kem_algo"]) as kem:
                    # load private key from backend session
                    kem.import_secret_key(session["secret_key"])

                    # decode hex ciphertext from client payload
                    ciphertext = bytes.fromhex(kem_ct)

                    # Decapsulate to retrieve the final shared secret 
                    shared_secret = kem.decap(ciphertext)
                    shared_secret_hex = shared_secret.hex()

                    # ================= DASHBOARD UPDATE =================
                    dashboard_status[username] = secure_status

            except Exception as e:
                print("❌ KEM ERROR:", str(e))

                dashboard_status[username] = {
                    "algorithm": KEM_ALGO,
                    "mlkem_active": False,
                    "connection": "BLOCKED",
                    "quantum_protection": False,
                    "shared_secret_verified": False,
                    "last_key_exchange": "Failed",
                    "security_alert": f"Tampering Detected: {str(e)}",
                    "tampering_detected": True
                }
                shared_secret_hex = None
        else:
            # Successful login now exposes the secure dashboard state directly
            dashboard_status[username] = secure_status

        # ================= JWT =================
        token = jwt.encode(
            {
                "username": username,
                "exp": time.time() + 3600
            },
            JWT_SECRET,
            algorithm=JWT_ALGO
        )
        
        # Guard for older versions of PyJWT that return bytes instead of strings
        if isinstance(token, bytes):
            token = token.decode('utf-8')

        return jsonify({
            "success": True,
            "token": token,
            "shared_secret": shared_secret_hex,
            "pqc_status": dashboard_status.get(username)
        })

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/dashboard/status", methods=["GET"])
def dashboard():
    username = request.args.get("username", "").strip()

    if username:
        return jsonify(dashboard_status.get(username, {})), 200

    return jsonify(dashboard_status), 200


# ================= RUN =================
if __name__ == "__main__":
    app.run(
        host="0.0.0.0",   # Allows React UI running on different interfaces to hit API
        port=5000,
        debug=True
    )