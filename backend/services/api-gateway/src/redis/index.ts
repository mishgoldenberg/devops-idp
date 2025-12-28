import { createClient } from 'redis';
import { config } from '../config';
import { createLogger } from '@devops-control-center/shared';

const logger = createLogger('api-gateway:redis');

export const redisClient = createClient({
  socket: {
    host: config.redis.host,
    port: config.redis.port,
  },
  password: config.redis.password,
});

redisClient.on('error', (err) => {
  logger.error('Redis error', { error: err.message });
});

redisClient.on('connect', () => {
  logger.info('Redis connected');
});

export async function initRedis() {
  await redisClient.connect();
}

export async function redisHealthCheck(): Promise<boolean> {
  try {
    await redisClient.ping();
    return true;
  } catch (error) {
    return false;
  }
}

// Cache helper functions
export async function cacheGet<T = any>(key: string): Promise<T | null> {
  try {
    const data = await redisClient.get(key);
    return data ? JSON.parse(data) : null;
  } catch (error: any) {
    logger.error('Cache get error', { key, error: error.message });
    return null;
  }
}

export async function cacheSet(key: string, value: any, ttlSeconds: number = 300): Promise<void> {
  try {
    await redisClient.setEx(key, ttlSeconds, JSON.stringify(value));
  } catch (error: any) {
    logger.error('Cache set error', { key, error: error.message });
  }
}

export async function cacheDel(key: string): Promise<void> {
  try {
    await redisClient.del(key);
  } catch (error: any) {
    logger.error('Cache delete error', { key, error: error.message });
  }
}

