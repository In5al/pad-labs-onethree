# Durak.md
## Application Suitability
### Why is this application relevant?
<p>Durak.md is a multiplayer card game that allows players to join a lobby, play games in real time, and keep track of their performance. A lobby system helps manage game sessions, allowing players to join, create, or leave games seamlessly. The application will offer real-time gameplay, messaging, and a score tracking feature.<p>
<p>The real-time nature of the game, combined with multiple simultaneous user interactions and different game states, makes Durak.md an ideal candidate for microservices. Microservices provide a scalable and fault-tolerant way to manage various game components like lobbies, user authentication, game logic, and score tracking.</p>

### Why does this application require a microservice architecture?
<p>"Similar to how Facebook uses microservices for its complex, multi-faceted platform,  Durak.md has several independent components that are best managed as microservices:<p>
  
- Lobby management for real-time communication using WebSockets.
- Game engine for handling the game logic, turns, and game states.
- User management for player authentication, profile, and session tracking.
- Score tracking for updating and displaying player rankings and stats.

<p>By using microservices, each service can be developed, deployed, and scaled independently. For example, the Lobby service can be scaled separately if many users are waiting to join games, without impacting the game engine or user services. This also ensures fault tolerance—if the Game Engine service fails, it won’t crash the Lobby or User Management services.<p>

## Service Boundaries
![Use Case](https://github.com/In5al/PAD/blob/main/Lab1/pad_diagram.jpg)

### Architecture

- API Gateway 
  - Functionality -Central entry point for client requests
  - Responsabilities:
      - Central entry point for client requests
      - Handle authentication and authorization
      - Load balancing

- Service Discovery
  - Functionality - Manages service registration and discovery
  - Responsabilities:
      - Keep track of available services and their locations
      - Assist in load balancing and fault tolerance
        
- User & Score Service
  - Functionality -Manages user data and game scores
  - Responsabilities:
      - User authentication and profile management
      - Store and retrieve user scores
      - Manage player rankings

- Game Service
  - Functionality -Handles core game logic
  - Responsabilities:
      - Game state management
      - Turn management
      - Game rules implementation
        
- Notification Service
  - Functionality -Manages in-game notifications
  - Responsabilities:
      - Send game invites
      - Update players on moves
      - Deliver real-time game updates

## Technology Stack and Communication Patterns

### API Gateway:
- Technology: Not specified, but likely a lightweight solution like Nginx or a cloud-native option
- Communication:
  - Receives HTTP requests from clients
  - Communicates via HTTP with other services
 

### Service Discovery:
-  Not specified, but could be something like Consul or etcd
- Communication: 
  -HTTP communication with API Gateway and other services

### User & Score Service:
- Backend: Python (Flask).
- Database: PostgreSQL for persistent data storage
- Communication:
    - HTTP for requests from API Gateway
    - gRPC for inter-service communication with Game Service

### Game Service:
- Backend: Not specified, but should be optimized for real-time operations
- Databases:
    - Redis for caching and real-time data
    - MongoDB for game state persistence
- Communication:
    - HTTP for requests from API Gateway
    - gRPC for communication with User & Score Service
    - WebSocket for real-time communication with clients (through API Gateway)
 
 ### Databases:
   - PostgreSQL: Used by User & Score Service for structured data
   - Redis: Used by Game Service for caching and real-time operations
   - MongoDB: Used by Game Service for flexible game state storage

### Client-Server Communication:
   - HTTP: For standard REST API calls through the API Gateway
   - WebSocket: For real-time game updates, directly connecting clients to the Game Service

## Data Management 

### Lobby Service
Endpoint: `/ws/lobby/join`
   - WebSocket Message
```json
{
  "action": "join_lobby",
  "data": {
    "user_id": "string",
    "lobby_id": "string"
  }
}
```


Endpoint: `/api/lobby/create`
   - **Method**:  POST
   - **Data Received**:
```json
{
  "user_id": "string",
  "game_type": "string"
}
```
- **Responses**:
  - **201**:
    ```json
       {
           "msg": "Lobby created successfully."
       }
       ```
  - **400**:
    ```json
       {
           "msg": "Invalid data."
       }
       ```

Endpoint: `/ws/lobby/leave`
   - WebSocket Message
```json
{
  "action": "leave_lobby",
  "data": {
    "user_id": "string",
    "lobby_id": "string"
  }
}
```

 ## Game Engine Service

Endpoint: `/api/game/start`
   - **Method**:  POST
   - **Data Received**:
```json
{
  "lobby_id": "string",
  "players": ["string"]
}

```
- **Responses**:
  - **201**:
    ```json
       {
           "msg": "Game started successfully."
       }
       ```
  - **400**:
    ```json
       {
           "msg": "Invalid lobby ID."
       }
       ```

Endpoint: `/api/game/move`
   - **Method**:  POST
   - **Data Received**:
```json
{
  "game_id": "string",
  "player_id": "string",
  "move": "object"
}
```
- **Responses**:
  - **200**:
    ```json
       {
           "msg": "Move accepted."
       }
       ```
  - **400**:
    ```json
       {
           "msg": "Invalid move."
       }
       ```
    
    
    ## User Service

Endpoint: `/api/users/auth/signup`
   - **Method**:  POST
   - **Data Received**:
```json
{
  "username": "string",
  "email": "string",
  "password": "string"
}
```
- **Responses**:
  - **201**:
    ```json
       {
           "msg": "User successfully created."
       }
       ```
  - **400**:
    ```json
       {
           "msg": "Invalid data."
       }
       ```

Endpoint: `/api/users/auth/signin`
   - **Method**:  POST
   - **Data Received**:
```json
{
  "email": "string",
  "password": "string"
}
```
- **Responses**:
  - **200**:
    ```json
       {
           "msg": "JWT token returned."
       }
       ```
  - **401**:
    ```json
       {
           "msg": "Invalid credentials."
       }
       ```
     ## Score Tracking Service

Endpoint: `/api/score/user/<user_id>`
   - **Method**:  GET
   - **Responses**:
     - **200**:
      ```json
         {
             "msg": " Returns user score"
         }
       ```
     - **404**:
      ```json
         {
             "msg": "User not found."
         }
       ```
      ## Deployment and Scaling

- Containerization: All services will be containerized using Docker, ensuring consistency across development and production environments.
- Orchestration: Kubernetes will be used for orchestrating and scaling the services, particularly the Lobby and Game Engine services. This allows for efficient resource allocation during peak gaming hours.
- Load Balancing: NGINX will serve as the ingress controller, providing load balancing between microservices and handling SSL termination.
      
