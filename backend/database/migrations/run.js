#!/usr/bin/env node

/**
 * Database migration runner
 * Executes schema.sql to create all tables, indexes, and constraints
 */

// Load environment variables from .env file (don't override existing env vars)
require('dotenv').config({ 
  path: require('path').join(__dirname, '../../../.env'),
  override: false 
});

const { Pool } = require('pg');
const fs = require('fs');
const path = require('path');

async function runMigrations() {
  // Build DATABASE_URL from env vars (expand variables if DATABASE_URL contains ${})
  let databaseUrl = process.env.DATABASE_URL;
  if (!databaseUrl || databaseUrl.includes('${')) {
    const user = encodeURIComponent(process.env.POSTGRES_USER || 'devops_user');
    const password = encodeURIComponent(process.env.POSTGRES_PASSWORD || 'devops_secure_password');
    const host = process.env.POSTGRES_HOST || 'localhost';
    const port = process.env.POSTGRES_PORT || 5432;
    const db = process.env.POSTGRES_DB || 'devops_control_center';
    databaseUrl = `postgresql://${user}:${password}@${host}:${port}/${db}`;
  }
  
  // Debug: log connection string (without password)
  const safeUrl = databaseUrl.replace(/:[^:@]+@/, ':****@');
  console.log(`🔗 Connecting to: ${safeUrl}`);
  
  // Try using individual parameters instead of connection string
  const pool = new Pool({
    user: process.env.POSTGRES_USER || 'devops_user',
    password: process.env.POSTGRES_PASSWORD || 'devops_secure_password',
    host: process.env.POSTGRES_HOST || 'localhost',
    port: parseInt(process.env.POSTGRES_PORT || '5432', 10),
    database: process.env.POSTGRES_DB || 'devops_control_center',
  });

  try {
    console.log('🔄 Running database migrations...');

    // Read schema file
    const schemaPath = path.join(__dirname, '..', 'schema.sql');
    const schema = fs.readFileSync(schemaPath, 'utf8');

    // Execute schema
    await pool.query(schema);

    console.log('✅ Migrations completed successfully');
  } catch (error) {
    console.error('❌ Migration failed:', error.message);
    process.exit(1);
  } finally {
    await pool.end();
  }
}

runMigrations();

