import pytest
from app import app, db, User
import json

@pytest.fixture
def client():
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    app.config['TESTING'] = True
    
    with app.test_client() as client:
        with app.app_context():
            db.create_all()
            yield client
        db.session.remove()
        db.drop_all()

def test_health_check(client):
    response = client.get('/ping')
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['status'] == 'healthy'

def test_signup(client):
    response = client.post('/api/users/auth/signup', json={
        'username': 'testuser',
        'email': 'test@test.com',
        'password': 'password123'
    })
    assert response.status_code == 201

def test_signin(client):
    # Create user
    client.post('/api/users/auth/signup', json={
        'username': 'testuser',
        'email': 'test@test.com',
        'password': 'password123'
    })
    
    # Test signin
    response = client.post('/api/users/auth/signin', json={
        'email': 'test@test.com',
        'password': 'password123'
    })
    assert response.status_code == 200