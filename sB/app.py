import os
from flask import Flask, request, jsonify, Response
from flask_pymongo import PyMongo
import redis
import grpc
from concurrent import futures
import time
from datetime import datetime
import json
from functools import wraps
import threading
from prometheus_client import Counter, Gauge, start_http_server
import logging
from typing import Dict, Any
import unittest
from unittest.mock import MagicMock, patch
import argparse

# Initialize Flask
app = Flask(__name__)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Database configurations
app.config['MONGO_URI'] = os.getenv('MONGO_URI', 'mongodb://mongodb:27017/gamedb')
mongo = PyMongo(app)

# Redis for game state caching
redis_client = redis.Redis(
    host=os.getenv('REDIS_HOST', 'redis'),
    port=int(os.getenv('REDIS_PORT', 6387)),  # Changed to default Redis port
    decode_responses=True
)

# Constants
MAX_CONCURRENT_REQUESTS = 100
REQUEST_TIMEOUT = 5  # seconds
ERROR_THRESHOLD = 3
RESET_TIMEOUT = 60  # seconds

# Metrics
REQUESTS = Counter('game_service_requests_total', 'Total requests')
ACTIVE_GAMES = Gauge('game_service_active_games', 'Number of active games')
ERROR_COUNTER = Counter('game_service_errors_total', 'Total errors')
REQUEST_LATENCY = Gauge('game_service_latency_seconds', 'Request latency')

# Circuit Breaker State
class CircuitBreaker:
    def __init__(self):
        self.errors = 0
        self.last_error_time = 0
        self.state = "CLOSED"
        self.lock = threading.Lock()

    def record_error(self):
        with self.lock:
            self.errors += 1
            self.last_error_time = time.time()
            if self.errors >= ERROR_THRESHOLD:
                self.state = "OPEN"
                logger.warning("Circuit breaker opened")

    def record_success(self):
        with self.lock:
            self.errors = 0
            if self.state == "OPEN" and (time.time() - self.last_error_time) > RESET_TIMEOUT:
                self.state = "CLOSED"
                logger.info("Circuit breaker closed")

    def is_open(self):
        return self.state == "OPEN"

circuit_breaker = CircuitBreaker()

# Decorators
def with_circuit_breaker(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if circuit_breaker.is_open():
            return jsonify({"error": "Service temporarily unavailable"}), 503
        
        try:
            start_time = time.time()
            result = func(*args, **kwargs)
            REQUEST_LATENCY.set(time.time() - start_time)
            circuit_breaker.record_success()
            return result
        except Exception as e:
            circuit_breaker.record_error()
            ERROR_COUNTER.inc()
            raise
    return wrapper

def with_timeout(timeout_seconds):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            start_time = time.time()
            while time.time() - start_time < timeout_seconds:
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if time.time() - start_time >= timeout_seconds:
                        return jsonify({"error": "Request timeout"}), 408
                    raise
            return jsonify({"error": "Request timeout"}), 408
        return wrapper
    return decorator

# Health Check Endpoint
@app.route('/status', methods=['GET'])
@with_circuit_breaker
@with_timeout(REQUEST_TIMEOUT)
def status() -> Response:
    """Health check endpoint with detailed metrics"""
    try:
        # Check MongoDB connection
        mongo.db.command('ping')
        
        # Check Redis connection
        redis_client.ping()
        
        metrics = {
            "status": "healthy",
            "timestamp": datetime.utcnow().isoformat(),
            "active_games": ACTIVE_GAMES._value.get(),
            "total_requests": REQUESTS._value.get(),
            "total_errors": ERROR_COUNTER._value.get(),
            "circuit_breaker_state": circuit_breaker.state,
            "db_connected": True,
            "cache_connected": True
        }
        
        return jsonify(metrics), 200
    except Exception as e:
        logger.error(f"Health check failed: {str(e)}")
        return jsonify({"status": "unhealthy", "error": str(e)}), 500

# Game Management Endpoints
@app.route('/api/game/start', methods=['POST'])
@with_circuit_breaker
@with_timeout(REQUEST_TIMEOUT)
def start_game() -> Response:
    """Start a new game session"""
    REQUESTS.inc()
    try:
        data = request.json
        if not data or 'lobby_id' not in data or 'players' not in data:
            return jsonify({"error": "Invalid request data"}), 400

        game = {
            'lobby_id': data['lobby_id'],
            'players': data['players'],
            'state': 'WAITING',
            'timestamp': datetime.utcnow().isoformat(),
            'moves': [],
            'deck': initialize_deck()
        }

        # Store in MongoDB
        result = mongo.db.games.insert_one(game)
        game_id = str(result.inserted_id)

        # Cache game state
        redis_client.setex(
            f"game:{game_id}",
            3600,  # 1 hour expiration
            json.dumps(game)
        )

        ACTIVE_GAMES.inc()
        return jsonify({
            "message": "Game created successfully",
            "game_id": game_id
        }), 201

    except Exception as e:
        logger.error(f"Error creating game: {str(e)}")
        return jsonify({"error": "Failed to create game"}), 500

@app.route('/api/game/move', methods=['POST'])
@with_circuit_breaker
@with_timeout(REQUEST_TIMEOUT)
def make_move() -> Response:
    """Process a game move"""
    REQUESTS.inc()
    try:
        data = request.json
        if not data or 'game_id' not in data or 'player_id' not in data or 'move' not in data:
            return jsonify({"error": "Invalid move data"}), 400

        # Get game state from cache
        game = redis_client.get(f"game:{data['game_id']}")
        if game:
            game = json.loads(game)
        else:
            # Fallback to MongoDB
            game = mongo.db.games.find_one({"_id": data['game_id']})
            if not game:
                return jsonify({"error": "Game not found"}), 404

        # Validate and process move
        if not is_valid_move(game, data['player_id'], data['move']):
            return jsonify({"error": "Invalid move"}), 400

        # Update game state
        game = update_game_state(game, data['player_id'], data['move'])

        # Save to MongoDB
        mongo.db.games.update_one(
            {"_id": data['game_id']},
            {"$set": game}
        )

        # Update cache
        redis_client.setex(
            f"game:{data['game_id']}",
            3600,
            json.dumps(game)
        )

        return jsonify({
            "message": "Move processed successfully",
            "game_state": game
        }), 200

    except Exception as e:
        logger.error(f"Error processing move: {str(e)}")
        return jsonify({"error": "Failed to process move"}), 500

# Game Logic Functions
def initialize_deck() -> list:
    """Initialize a new deck of cards"""
    ranks = ['6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A']
    suits = ['hearts', 'diamonds', 'clubs', 'spades']
    return [{'rank': rank, 'suit': suit} for rank in ranks for suit in suits]

def is_valid_move(game: Dict[str, Any], player_id: str, move: Dict[str, Any]) -> bool:
    """Validate if the move is legal"""
    # Add your game-specific move validation logic here
    return True

def update_game_state(game: Dict[str, Any], player_id: str, move: Dict[str, Any]) -> Dict[str, Any]:
    """Update game state based on the move"""
    game['moves'].append({
        'player_id': player_id,
        'move': move,
        'timestamp': datetime.utcnow().isoformat()
    })
    return game

# Unit Tests
class GameServiceTests(unittest.TestCase):
    def setUp(self):
        self.app = app.test_client()
        self.mongo_patcher = patch('flask_pymongo.PyMongo')
        self.redis_patcher = patch('redis.Redis')
        self.mock_mongo = self.mongo_patcher.start()
        self.mock_redis = self.redis_patcher.start()

    def tearDown(self):
        self.mongo_patcher.stop()
        self.redis_patcher.stop()

    def test_status_endpoint(self):
        response = self.app.get('/status')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertEqual(data['status'], 'healthy')

    def test_start_game(self):
        test_data = {
            'lobby_id': 'test_lobby',
            'players': ['player1', 'player2']
        }
        response = self.app.post('/api/game/start', json=test_data)
        self.assertEqual(response.status_code, 201)
        data = json.loads(response.data)
        self.assertIn('game_id', data)

    def test_make_move(self):
        test_data = {
            'game_id': 'test_game',
            'player_id': 'player1',
            'move': {'card': {'rank': '7', 'suit': 'hearts'}}
        }
        response = self.app.post('/api/game/move', json=test_data)
        self.assertEqual(response.status_code, 200)

if __name__ == '__main__':
    import socket
    hostname = socket.gethostname()
    container_port = 5024
    
    # Start Flask app
    app.run(
        host='0.0.0.0',
        port=container_port,
        threaded=True
    )