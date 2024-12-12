#!/bin/bash
set -e

# Wait for MongoDB to be ready
until nc -z mongodb 27017; do
  echo "Waiting for MongoDB to be ready..."
  sleep 1
done

# Wait for Redis to be ready
until nc -z redis 6387; do
  echo "Waiting for Redis to be ready..."
  sleep 1
done

# Start the application
exec python app.py