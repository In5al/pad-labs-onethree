from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_jwt_extended import JWTManager, create_access_token, jwt_required, get_jwt_identity
from werkzeug.security import generate_password_hash, check_password_hash
from flask_cors import CORS
import requests
import threading
import time
import os
import logging
import redis
from redis.exceptions import RedisError
from functools import wraps
import grpc
from concurrent import futures
from sqlalchemy import text
from requests.exceptions import RequestException
from flask_socketio import SocketIO, emit, join_room, leave_room
import json

# Initialize Flask app
app = Flask(__name__)
CORS(app)

# Initialize SocketIO
socketio = SocketIO(app, cors_allowed_origins="*")

# Lobby storage
active_lobbies = {}
user_sessions = {}

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'postgresql://user:password@postgres:5432/userdb')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['JWT_SECRET_KEY'] = os.getenv('JWT_SECRET_KEY', 'your-secret-key')
app.config['REQUEST_TIMEOUT'] = int(os.getenv('REQUEST_TIMEOUT', 5))
GATEWAY_SECRET = os.getenv('GATEWAY_SECRET', 'your-gateway-secret')

# Redis Configuration
REDIS_HOST = os.getenv('REDIS_HOST', 'api-gateway-redis-1')
REDIS_PORT = int(os.getenv('REDIS_PORT', 6379))
MAX_CONCURRENT_REQUESTS = int(os.getenv('MAX_CONCURRENT_REQUESTS', 100))
current_requests = 0

# Initialize extensions
db = SQLAlchemy(app)
jwt = JWTManager(app)

# Initialize Redis
try:
    redis_client = redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        decode_responses=True,
        socket_connect_timeout=5
    )
    redis_client.ping()
    logger.info("Redis connection successful")
except RedisError as e:
    logger.error(f"Redis connection failed: {e}")
    redis_client = None


class Lobby:
    def __init__(self, lobby_id, host_id):
        self.lobby_id = lobby_id
        self.host_id = host_id
        self.players = {host_id: {"ready": False}}
        self.max_players = 4
        self.status = "waiting"

@socketio.on('connect')
def handle_connect():
    token = request.args.get('token')
    try:
        # Verify JWT token
        user_id = verify_jwt_token(token)
        user_sessions[request.sid] = user_id
        emit('connection_success', {'message': 'Connected to lobby service'})
    except Exception as e:
        return False  # Reject connection

@socketio.on('create_lobby')
def handle_create_lobby(data):
    user_id = user_sessions.get(request.sid)
    if not user_id:
        emit('error', {'message': 'Not authenticated'})
        return

    lobby_id = generate_lobby_id()
    new_lobby = Lobby(lobby_id, user_id)
    active_lobbies[lobby_id] = new_lobby
    
    join_room(lobby_id)
    emit('lobby_created', {
        'lobby_id': lobby_id,
        'host_id': user_id
    })

@socketio.on('join_lobby')
def handle_join_lobby(data):
    lobby_id = data.get('lobby_id')
    user_id = user_sessions.get(request.sid)
    
    if not lobby_id or not user_id:
        emit('error', {'message': 'Invalid request'})
        return
        
    lobby = active_lobbies.get(lobby_id)
    if not lobby:
        emit('error', {'message': 'Lobby not found'})
        return
        
    if len(lobby.players) >= lobby.max_players:
        emit('error', {'message': 'Lobby is full'})
        return
    
    lobby.players[user_id] = {"ready": False}
    join_room(lobby_id)
    
    emit('player_joined', {
        'user_id': user_id,
        'lobby_info': {
            'players': list(lobby.players.keys()),
            'host_id': lobby.host_id,
            'status': lobby.status
        }
    }, room=lobby_id)

@socketio.on('player_ready')
def handle_player_ready(data):
    lobby_id = data.get('lobby_id')
    user_id = user_sessions.get(request.sid)
    
    lobby = active_lobbies.get(lobby_id)
    if lobby and user_id in lobby.players:
        lobby.players[user_id]["ready"] = True
        
        all_ready = all(player["ready"] for player in lobby.players.values())
        if all_ready:
            lobby.status = "starting"
            emit('game_starting', {
                'lobby_id': lobby_id,
                'players': list(lobby.players.keys())
            }, room=lobby_id)

@socketio.on('leave_lobby')
def handle_leave_lobby(data):
    lobby_id = data.get('lobby_id')
    user_id = user_sessions.get(request.sid)
    
    lobby = active_lobbies.get(lobby_id)
    if lobby and user_id in lobby.players:
        leave_room(lobby_id)
        del lobby.players[user_id]
        
        if len(lobby.players) == 0:
            del active_lobbies[lobby_id]
        elif user_id == lobby.host_id:
            # Assign new host
            lobby.host_id = next(iter(lobby.players))
        
        emit('player_left', {
            'user_id': user_id,
            'new_host_id': lobby.host_id
        }, room=lobby_id)

# Helper functions
def generate_lobby_id():
    import uuid
    return str(uuid.uuid4())[:8]

def verify_jwt_token(token):
    # Use your existing JWT verification logic
    pass

# Circuit Breaker Configuration
FAILURE_THRESHOLD = 3
RECOVERY_TIMEOUT = 60
circuit_state = {
    'failures': 0,
    'last_failure': 0,
    'status': 'CLOSED'
}


# Database Model
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    score = db.Column(db.Integer, default=0)
    games_played = db.Column(db.Integer, default=0)
    games_won = db.Column(db.Integer, default=0)

    def __init__(self, username, email, password):
        self.username = username
        self.email = email
        self.password_hash = generate_password_hash(password)

# Middleware
def verify_gateway_request():
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            # Skip verification for health check endpoint
            if request.path == '/ping':
                return f(*args, **kwargs)
            
            gateway_token = request.headers.get('X-Gateway-Token')
            if not gateway_token or gateway_token != GATEWAY_SECRET:
                return jsonify({'error': 'Unauthorized'}), 401
            
            return f(*args, **kwargs)
        return decorated_function
    return decorator

@app.before_request
def before_request():
    # Skip verification for health checks
    if request.path == '/ping':
        return None
    
    print("Request Path:", request.path)
    print("Request Headers:", request.headers)
    print("Gateway Secret:", GATEWAY_SECRET)
    
    gateway_token = request.headers.get('X-Gateway-Token')
    print("Received Gateway Token:", gateway_token)
    
    if not gateway_token or gateway_token != GATEWAY_SECRET:
        print("Gateway token mismatch or missing")
        return jsonify({'error': 'Unauthorized'}), 401

    # For debugging JWT token
    auth_header = request.headers.get('Authorization')
    print("Auth Header:", auth_header)

    print("\n=== Request Debug Info ===")
    print(f"Path: {request.path}")
    print(f"Method: {request.method}")
    print(f"Headers: {dict(request.headers)}")
    print(f"Expected Gateway Secret: {GATEWAY_SECRET}")
    print("==========================\n")

@app.after_request
def after_request(response):
    global current_requests
    current_requests -= 1
    return response

# Circuit Breaker Decorator
def circuit_breaker(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if circuit_state['status'] == 'OPEN':
            if time.time() - circuit_state['last_failure'] > RECOVERY_TIMEOUT:
                circuit_state['status'] = 'HALF-OPEN'
            else:
                return jsonify({'error': 'Service temporarily unavailable'}), 503

        try:
            result = func(*args, **kwargs)
            if circuit_state['status'] == 'HALF-OPEN':
                circuit_state['status'] = 'CLOSED'
                circuit_state['failures'] = 0
            return result
        except Exception as e:
            circuit_state['failures'] += 1
            circuit_state['last_failure'] = time.time()
            if circuit_state['failures'] >= FAILURE_THRESHOLD:
                circuit_state['status'] = 'OPEN'
            raise e
    return wrapper

# Modify the service registration function
def register_with_gateway():
    while True:
        try:
            headers = {'X-Gateway-Token': GATEWAY_SECRET}
            
            response = requests.post(
                'http://api-gateway:8080/sA/register',
                headers=headers,
                json={
                    'host': 'service-a:5000',  # Make sure this matches Docker service name
                    'serviceType': 'A'
                },
                timeout=5
            )
            logger.info(f"Registration response: {response.status_code}")
            time.sleep(30)
        except Exception as e:
            logger.error(f"Registration failed: {str(e)}")
            time.sleep(5)

# Database initialization
def init_db():
    retry_count = 0
    max_retries = 5
    retry_delay = 5

    while retry_count < max_retries:
        try:
            with app.app_context():
                db.create_all()
                db.session.execute(text('SELECT 1'))
                db.session.commit()
                logger.info("Database initialized successfully")
                return True
        except Exception as e:
            retry_count += 1
            logger.error(f"Database initialization attempt {retry_count} failed: {e}")
            if retry_count < max_retries:
                time.sleep(retry_delay)
            else:
                logger.error("Max retries reached. Database initialization failed.")
                return False

# Endpoints


@app.route('/ping', methods=['GET'])
def health_check():
    try:
        db.session.execute(text('SELECT 1'))
        db_status = 'healthy'
    except Exception:
        db_status = 'unhealthy'

    try:
        redis_client.ping()
        redis_status = 'healthy'
    except Exception:
        redis_status = 'unhealthy'

    return jsonify({
        'service': 'User & Score Service',
        'status': 'healthy',
        'database': db_status,
        'redis': redis_status,
        'circuit_breaker': circuit_state['status'],
        'current_requests': current_requests
    }), 200

@app.route('/api/users/auth/signup', methods=['POST'])
@verify_gateway_request()
@circuit_breaker
def signup():
    try:
    #debug
        print("=== Signup Request ===")
        print(f"Headers: {dict(request.headers)}")
        print(f"Body: {request.get_json()}")
        print(f"Gateway Secret: {GATEWAY_SECRET}")
        print("=====================")

        data = request.get_json()
        if not data or not all(k in data for k in ['username', 'email', 'password']):
            return jsonify({'error': 'Missing required fields'}), 400

        if User.query.filter_by(email=data['email']).first():
            return jsonify({'error': 'Email already registered'}), 409

        if User.query.filter_by(username=data['username']).first():
            return jsonify({'error': 'Username already taken'}), 409

        new_user = User(
            username=data['username'],
            email=data['email'],
            password=data['password']
        )
        
        db.session.add(new_user)
        db.session.commit()

        return jsonify({
            'message': 'User created successfully',
            'user_id': new_user.id
        }), 201

    except Exception as e:
        db.session.rollback()
        logger.error(f"Signup error: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/users/auth/signin', methods=['POST'])
@verify_gateway_request()
@circuit_breaker
def signin():
    try:
        data = request.get_json()
        if not data or not all(k in data for k in ['email', 'password']):
            return jsonify({'error': 'Missing email or password'}), 400

        user = User.query.filter_by(email=data['email']).first()
        if not user or not check_password_hash(user.password_hash, data['password']):
            return jsonify({'error': 'Invalid credentials'}), 401

        access_token = create_access_token(identity=user.id)
        return jsonify({
            'access_token': access_token,
            'user_id': user.id,
            'username': user.username
        }), 200

    except Exception as e:
        logger.error(f"Signin error: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/score/user/<int:user_id>', methods=['GET'])
@verify_gateway_request()
@jwt_required()
@circuit_breaker
def get_user_score(user_id):
    try:
        user = User.query.get(user_id)
        if not user:
            return jsonify({'error': 'User not found'}), 404

        return jsonify({
            'user_id': user.id,
            'username': user.username,
            'score': user.score,
            'games_played': user.games_played,
            'games_won': user.games_won
        }), 200

    except Exception as e:
        logger.error(f"Error fetching score: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/score/update', methods=['POST'])
@verify_gateway_request()
@jwt_required()
@circuit_breaker
def update_score():
    try:
        data = request.get_json()
        if not data or 'user_id' not in data or 'score_change' not in data:
            return jsonify({'error': 'Missing required fields'}), 400

        user = User.query.get(data['user_id'])
        if not user:
            return jsonify({'error': 'User not found'}), 404

        user.score += data['score_change']
        if data.get('game_won', False):
            user.games_won += 1
        user.games_played += 1

        db.session.commit()

        return jsonify({
            'message': 'Score updated successfully',
            'new_score': user.score,
            'games_played': user.games_played,
            'games_won': user.games_won
        }), 200

    except Exception as e:
        db.session.rollback()
        logger.error(f"Error updating score: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

# Error handlers
@app.errorhandler(404)
def not_found_error(error):
    return jsonify({'error': 'Resource not found'}), 404

@app.errorhandler(500)
def internal_error(error):
    db.session.rollback()
    return jsonify({'error': 'Internal server error'}), 500

if __name__ == '__main__':
    if init_db():
        # Start service registration in background
        registration_thread = threading.Thread(target=register_with_gateway, daemon=True)
        registration_thread.start()
        
        # Start Flask app
        socketio.run(app, host='0.0.0.0', port=5000)
    else:
        logger.error("Failed to initialize database. Exiting.")
        exit(1)