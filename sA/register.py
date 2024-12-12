import redis
import time
import os

def register_service():
    print("Starting service registration...")
    # Get Redis connection details from environment variables
    redis_host = os.getenv('REDIS_HOST', 'api-gateway-redis-1')
    redis_port = int(os.getenv('REDIS_PORT', 6379))
    print(f"Will connect to Redis at {redis_host}:{redis_port}")
    
    while True:
        try:
            print(f"Attempting to connect to Redis at {redis_host}:{redis_port}")
            redis_client = redis.Redis(
                host=redis_host,
                port=redis_port,
                decode_responses=True,
                socket_connect_timeout=5
            )
            redis_client.ping()
            print("Successfully connected to Redis")
            break
        except Exception as e:
            print(f"Failed to connect to Redis: {str(e)}")
            print("Waiting for Redis...")
            time.sleep(5)
    
    service_address = "service-a:5000"
    try:
        redis_client.delete('service:A')
        print("Cleared old registrations")
        redis_client.lpush('service:A', service_address)
        print(f"Successfully registered service at {service_address}")
        registered = redis_client.lrange('service:A', 0, -1)
        print(f"Current registrations: {registered}")
    except Exception as e:
        print(f"Error during registration: {str(e)}")

if __name__ == "__main__":
    register_service()