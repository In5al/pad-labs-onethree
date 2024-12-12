const express = require('express');
const axios = require('axios');
const redis = require('redis');
const winston = require('winston');
const grpc = require('@grpc/grpc-js');
const protoLoader = require('@grpc/proto-loader');
const path = require('path');

//Lab2
const prometheus = require('prom-client');
const register = new prometheus.Registry();

require('dotenv').config();

// Configuration
const PORT = process.env.PORT || 8080;
const SM_REDIS_URL = process.env.SM_REDIS_URL || 'redis://localhost:6379';
const SERV_REST_PORT = process.env.SERV_REST_PORT || 5000;
const SERVER_TIMEOUT_MS = parseInt(process.env.SERVER_TIMEOUT_MS) || 5000;
const MAX_CONCURRENT_REQUESTS = parseInt(process.env.MAX_CONCURRENT_REQUESTS) || 100;
const ERROR_THRESHOLD = parseInt(process.env.ERROR_THRESHOLD) || 3;
const ERROR_TIMEOUT = parseInt(process.env.ERROR_TIMEOUT) || 17500;
const CRITICAL_LOAD_THRESHOLD = parseInt(process.env.CRITICAL_LOAD_THRESHOLD) || 60;
const REROUTE_THRESHOLD = parseInt(process.env.REROUTE_THRESHOLD) || 2; // New threshold for reroutes


let concurrentRequests = 0;

// Initialize Express
const app = express();
app.use(express.json());

// Initialize Winston Logger
const logger = winston.createLogger({
    level: 'info',
    format: winston.format.combine(
        winston.format.timestamp(),
        winston.format.json()
    ),
    transports: [
        new winston.transports.File({ filename: 'error.log', level: 'error' }),
        new winston.transports.File({ filename: 'combined.log' })
    ]
});

if (process.env.NODE_ENV !== 'production') {
    logger.add(new winston.transports.Console({
        format: winston.format.simple()
    }));
}

// Prometheus metrics l2
const httpRequestDuration = new prometheus.Histogram({
    name: 'http_request_duration_seconds',
    help: 'Duration of HTTP requests in seconds',
    labelNames: ['method', 'route', 'status_code'],
    buckets: [0.1, 0.5, 1, 2, 5]
});

const serviceHealthGauge = new prometheus.Gauge({
    name: 'service_health_status',
    help: 'Health status of services (1 = healthy, 0 = unhealthy)',
    labelNames: ['service']
});

const activeConnections = new prometheus.Gauge({
    name: 'active_connections',
    help: 'Number of active connections'
});

const circuitBreakerStatus = new prometheus.Gauge({
    name: 'circuit_breaker_status',
    help: 'Circuit breaker status (0=closed, 1=open, 2=half-open)',
    labelNames: ['service']
});

// Register metrics
register.setDefaultLabels({
    app: 'api_gateway'
});
prometheus.collectDefaultMetrics({ register });
register.registerMetric(httpRequestDuration);
register.registerMetric(serviceHealthGauge);
register.registerMetric(activeConnections);
register.registerMetric(circuitBreakerStatus);

//end

// Circuit Breaker State
// Circuit Breaker State with reroute tracking
const circuitBreakers = {
    serviceA: { 
        failures: 0, 
        lastFailure: null, 
        state: 'CLOSED',
        reroutes: 0,
        lastReroute: null,
        consecutiveReroutes: 0
    },
    serviceB: { 
        failures: 0, 
        lastFailure: null, 
        state: 'CLOSED',
        reroutes: 0,
        lastReroute: null,
        consecutiveReroutes: 0
    }
};

// Service Load Tracking
const serviceLoads = {
    serviceA: new Map(),
    serviceB: new Map()
};

// Redis Client with retry strategy and error handling
const redisClient = redis.createClient({
    url: SM_REDIS_URL,
    retry_strategy: function(options) {
        if (options.total_retry_time > 1000 * 60 * 60) {
            return null;
        }
        if (options.attempt > 10) {
            return null;
        }
        return Math.min(options.attempt * 100, 3000);
    }
});

let redisConnected = false;

redisClient.on('connect', () => {
    redisConnected = true;
    logger.info('Redis client connected');
});

redisClient.on('error', (err) => {
    redisConnected = false;
    logger.error('Redis client error:', err);
});

// Modify the connection to use async/await with error handling
(async () => {
    try {
        await redisClient.connect();
    } catch (err) {
        logger.error('Failed to connect to Redis:', err);
        // Service can still start without Redis
        logger.info('API Gateway will start without Redis connection');
    }
})();

// Helper function to safely interact with Redis
async function safeRedisOperation(operation) {
    if (!redisConnected) {
        logger.warn('Redis not connected, returning empty result');
        return [];
    }
    try {
        return await operation();
    } catch (error) {
        logger.error('Redis operation failed:', error);
        return [];
    }
}

// Update getServiceIPs function
async function getServiceIPs(serviceType) {
    return safeRedisOperation(async () => {
        const redisKey = `service:${serviceType}`;
        return await redisClient.lRange(redisKey, 0, -1);
    });
}

// Add this function right after getServiceIPs
async function getAllServices() {
    try {
        const serviceA = await getServiceIPs('A');
        const serviceB = await getServiceIPs('B');
        return {
            'A': serviceA,
            'B': serviceB
        };
    } catch (error) {
        logger.error('Error getting all services:', error);
        return {
            'A': [],
            'B': []
        };
    }
}

// Health Check Cache
const healthCheckCache = {
    timestamp: null,
    results: null
};


// Enhanced Service Health Monitoring
class ServiceHealthMonitor {
    constructor() {
        this.healthStatus = new Map();
        this.startHealthChecks();
    }

    async checkServiceHealth(serviceType, instance) {
        try {
            const response = await axios.get(`http://${instance}/ping`, {
                timeout: SERVER_TIMEOUT_MS
            });
            return response.status === 200;
        } catch (error) {
            logger.error(`Health check failed for ${serviceType} at ${instance}:`, error);
            return false;
        }
    }

    async updateServiceHealth(serviceType, instance) {
        const isHealthy = await this.checkServiceHealth(serviceType, instance);
        const key = `${serviceType}-${instance}`;
        this.healthStatus.set(key, isHealthy);
        serviceHealthGauge.set({ service: key }, isHealthy ? 1 : 0);
        return isHealthy;
    }

    startHealthChecks() {
        setInterval(async () => {
            const services = await getAllServices();
            for (const [serviceType, instances] of Object.entries(services)) {
                for (const instance of instances) {
                    await this.updateServiceHealth(serviceType, instance);
                }
            }
        }, 30000); // Check every 30 seconds
    }

    isHealthy(serviceType, instance) {
        return this.healthStatus.get(`${serviceType}-${instance}`) ?? false;
    }
}

const healthMonitor = new ServiceHealthMonitor();


// Circuit Breaker Functions
// Enhanced Circuit Breaker Functions l2 improvemnt
function checkCircuitBreaker(service) {
    const breaker = circuitBreakers[service];
    
    if (breaker.state === 'OPEN') {
        const now = Date.now();
        if (now - breaker.lastFailure > ERROR_TIMEOUT) {
            breaker.state = 'HALF-OPEN';
            breaker.consecutiveReroutes = 0; // Reset reroutes on recovery attempt
            logger.info(`Circuit breaker for ${service} entering HALF-OPEN state`);
            return true;
        }
        return false;
    }
    return true;
}

function recordReroute(service) {
    const breaker = circuitBreakers[service];
    const now = Date.now();
    
    // Check if this is a consecutive reroute (within 5 seconds)
    if (breaker.lastReroute && (now - breaker.lastReroute) <= 5000) {
        breaker.consecutiveReroutes++;
    } else {
        breaker.consecutiveReroutes = 1;
    }
    
    breaker.reroutes++;
    breaker.lastReroute = now;
    
    // Trip circuit breaker if too many consecutive reroutes
    if (breaker.consecutiveReroutes >= REROUTE_THRESHOLD) {
        breaker.state = 'OPEN';
        breaker.lastFailure = now;
        logger.warn(`Circuit breaker for ${service} OPENED due to ${breaker.consecutiveReroutes} consecutive reroutes`);
        return false;
    }
    
    return true;
}

function recordFailure(service) {
    const breaker = circuitBreakers[service];
    const now = Date.now();
    
    breaker.failures++;
    breaker.lastFailure = now;
    
    if (breaker.failures >= ERROR_THRESHOLD) {
        if (now - breaker.lastFailure <= ERROR_TIMEOUT) {
            breaker.state = 'OPEN';
            logger.warn(`Circuit breaker for ${service} OPENED due to ${breaker.failures} failures`);
        } else {
            breaker.failures = 1;
        }
    }
}

function recordSuccess(service) {
    const breaker = circuitBreakers[service];
    if (breaker.state === 'HALF-OPEN') {
        breaker.state = 'CLOSED';
        breaker.failures = 0;
        breaker.lastFailure = null;
        breaker.consecutiveReroutes = 0; // Reset reroutes on successful recovery
        logger.info(`Circuit breaker for ${service} CLOSED after recovery`);
    }
}

// Enhanced service routing with circuit breaker and reroute tracking
async function routeToService(serviceType, req, res) {
    const serviceName = `service${serviceType}`;
    
    if (!checkCircuitBreaker(serviceName)) {
        return res.status(503).json({ 
            detail: `${serviceName} is currently unavailable (Circuit Breaker: OPEN)` 
        });
    }

    try {
        const serviceIPs = await getServiceIPs(serviceType);
        if (!serviceIPs?.length) {
            return res.status(503).json({ 
                detail: `${serviceName} is not available or Redis is disconnected` 
            });
        }

        const selectedInstance = await selectServiceInstance(serviceType, serviceIPs);
        if (!selectedInstance) {
            return res.status(503).json({ 
                detail: `No available instances for ${serviceName}` 
            });
        }

        console.log('Original URL:', req.url);
        const serviceUrl = `http://${selectedInstance}:${SERV_REST_PORT}${req.url}`;
        console.log('Routing to:', serviceUrl);

        const response = await axios({
            method: req.method,
            url: serviceUrl,
            data: req.body,
            headers: {
                ...req.headers,
                'X-Gateway-Token': process.env.GATEWAY_SECRET || 'test123'
            },
            timeout: SERVER_TIMEOUT_MS
        });

        recordSuccess(serviceName);
        return res.status(response.status).send(response.data);
    } catch (error) {
        recordFailure(serviceName);
        logger.error(`Error routing to ${serviceName}:`, error);
        
        if (error.code === 'ECONNABORTED') {
            return res.status(504).json({ detail: "Request timed out" });
        }
        return res.status(error.response?.status || 500).json({ 
            detail: error.response?.data?.detail || error.message 
        });
    }
}


// Load Balancing Functions
async function updateServiceLoad(serviceType, instance) {
    try {
        const response = await axios.get(`http://${instance}:${SERV_REST_PORT}/metrics`, {
            timeout: SERVER_TIMEOUT_MS
        });
        
        const load = response.data;
        serviceLoads[serviceType].set(instance, load);
        
        if (load.requestsPerSecond > CRITICAL_LOAD_THRESHOLD) {
            logger.warn(`ALERT: High load detected on ${serviceType} instance ${instance}`, {
                load,
                threshold: CRITICAL_LOAD_THRESHOLD
            });
        }
        
        return load;
    } catch (error) {
        logger.error(`Failed to get load for ${instance}:`, error);
        return null;
    }
}

// Enhanced Load Balancing with Health Checks
async function selectServiceInstance(serviceType, instances) {
    if (!instances || instances.length === 0) {
        return null;
    }

    try {
        // Filter healthy instances
        const healthyInstances = instances.filter(instance => 
            healthMonitor.isHealthy(serviceType, instance)
        );

        if (healthyInstances.length === 0) {
            logger.warn(`No healthy instances available for ${serviceType}`);
            // Fallback to any instance if all are unhealthy
            return instances[0];
        }

        // Update loads for healthy instances
        await Promise.all(healthyInstances.map(instance => 
            updateServiceLoad(serviceType, instance)
        ));
        
        const serviceLoadMap = serviceLoads[serviceType];
        if (!serviceLoadMap || !(serviceLoadMap instanceof Map)) {
            return healthyInstances[0];
        }

        // Sort by load with health consideration
        const loads = Array.from(serviceLoadMap.entries())
            .filter(([instance]) => healthyInstances.includes(instance))
            .sort((a, b) => {
                const loadA = a[1]?.requestsPerSecond ?? Infinity;
                const loadB = b[1]?.requestsPerSecond ?? Infinity;
                return loadA - loadB;
            });
        
        return loads[0]?.[0] || healthyInstances[0];
    } catch (error) {
        logger.error('Error in selectServiceInstance:', error);
        return instances[0];
    }
}


// Middleware for limiting concurrent tasks
const taskLimiter = (req, res, next) => {
    if (concurrentRequests >= MAX_CONCURRENT_REQUESTS) {
        return res.status(503).json({ detail: "API Gateway is busy. Please try again later." });
    }
    concurrentRequests++;
    res.on('finish', () => {
        concurrentRequests--;
    });
    next();
};

// Add this near your route definitions
app.post('/sA/register', async (req, res) => {
    try {
        const { host, serviceType } = req.body;
        console.log('Service registration attempt:', { host, serviceType });
        
        if (!host || !serviceType) {
            return res.status(400).json({ error: 'Missing required fields' });
        }

        await redisClient.lPush(`service:${serviceType}`, host);
        res.json({ status: 'registered' });
    } catch (error) {
        console.error('Registration error:', error);
        res.status(500).json({ error: 'Registration failed' });
    }
});

// Enhanced request handling middleware with metrics
app.use((req, res, next) => {
    const start = Date.now();
    activeConnections.inc();

    res.on('finish', () => {
        const duration = (Date.now() - start) / 1000;
        httpRequestDuration.observe(
            {
                method: req.method,
                route: req.route?.path || req.path,
                status_code: res.statusCode
            },
            duration
        );
        activeConnections.dec();
    });

    next();
});

// Update circuit breaker status in metrics
function updateCircuitBreakerMetrics(service) {
    const breaker = circuitBreakers[service];
    const statusValue = breaker.state === 'CLOSED' ? 0 : 
                       breaker.state === 'OPEN' ? 1 : 2;
    circuitBreakerStatus.set({ service }, statusValue);
}

// Modify recordFailure to update metrics
function recordFailure(service) {
    const breaker = circuitBreakers[service];
    const now = Date.now();
    
    breaker.failures++;
    breaker.lastFailure = now;
    
    if (breaker.failures >= ERROR_THRESHOLD) {
        if (now - breaker.lastFailure <= ERROR_TIMEOUT) {
            breaker.state = 'OPEN';
            logger.warn(`Circuit breaker for ${service} OPENED due to ${breaker.failures} failures`);
            updateCircuitBreakerMetrics(service);
        } else {
            breaker.failures = 1;
        }
    }
}

// Metrics endpoint
app.get('/metrics', async (req, res) => {
    try {
        res.set('Content-Type', register.contentType);
        res.end(await register.metrics());
    } catch (error) {
        logger.error('Error generating metrics:', error);
        res.status(500).end();
    }
});
// Enhanced status endpoint with health checks
app.get('/ping', async (req, res) => {
    const now = Date.now();
    
    if (healthCheckCache.timestamp && now - healthCheckCache.timestamp < 10000) {
        return res.status(200).json(healthCheckCache.results);
    }

    try {
        const [serviceAIPs, serviceBIPs] = await Promise.all([
            getServiceIPs('A'),  // Using the safe operation
            getServiceIPs('B')   // Using the safe operation
        ]);

        const healthStatus = {
            status: 'healthy',
            timestamp: now,
            gateway: {
                port: PORT,
                concurrentRequests,
                maxConcurrentRequests: MAX_CONCURRENT_REQUESTS,
                redisConnected  // Add Redis connection status
            },

            services: {
                serviceA: {
                    instances: serviceAIPs.length,
                    circuitBreakerState: circuitBreakers.serviceA.state,
                    healthStatus: await Promise.all(serviceAIPs.map(async ip => {
                        try {
                            await axios.get(`http://${ip}:${SERV_REST_PORT}/ping`, { 
                                timeout: SERVER_TIMEOUT_MS 
                            });
                            return { ip, status: 'healthy' };
                        } catch (error) {
                            return { ip, status: 'unhealthy', error: error.message };
                        }
                    }))
                },
                serviceB: {
                    instances: serviceBIPs.length,
                    circuitBreakerState: circuitBreakers.serviceB.state,
                    healthStatus: await Promise.all(serviceBIPs.map(async ip => {
                        try {
                            await axios.get(`http://${ip}:${SERV_REST_PORT}/ping`, { 
                                timeout: SERVER_TIMEOUT_MS 
                            });
                            return { ip, status: 'healthy' };
                        } catch (error) {
                            return { ip, status: 'unhealthy', error: error.message };
                        }
                    }))
                }
            }
        };

        // Cache results
        healthCheckCache.timestamp = now;
        healthCheckCache.results = healthStatus;

        res.status(200).json(healthStatus);
    } catch (error) {
        logger.error('Health check failed:', error);
        res.status(500).json({
            status: 'unhealthy',
            error: error.message
        });
    }
});

// Service routes
app.use('/sA/api/users/auth', taskLimiter, (req, res) => routeToService('A', req, res));
app.use('/sB', taskLimiter, (req, res) => routeToService('B', req, res));

// Graceful shutdown
async function shutdown(signal) {
    logger.info(`Received ${signal}. Starting graceful shutdown...`);
    
    try {
        await redisClient.quit();
        logger.info('Redis connection closed');
        
        server.close(() => {
            logger.info('Express server closed');
            process.exit(0);
        });
    } catch (error) {
        logger.error('Error during shutdown:', error);
        process.exit(1);
    }
}

['SIGINT', 'SIGTERM'].forEach(signal => {
    process.on(signal, () => shutdown(signal));
});

// Start server
const server = app.listen(PORT, () => {
    logger.info(`API Gateway listening at http://localhost:${PORT}`);
});

module.exports = app;