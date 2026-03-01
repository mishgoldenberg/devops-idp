#!/usr/bin/env node

/**
 * Database seeder
 * Populates database with initial data (roles, users, widget types, etc.)
 */

// Load environment variables from .env file (don't override existing env vars)
require('dotenv').config({ 
  path: require('path').join(__dirname, '../../../.env'),
  override: false 
});

const { Pool } = require('pg');
const fs = require('fs');
const path = require('path');

async function runSeeds() {
  // Build DATABASE_URL from env vars (expand variables if DATABASE_URL contains ${})
  let databaseUrl = process.env.DATABASE_URL;
  if (!databaseUrl || databaseUrl.includes('${')) {
    databaseUrl = `postgresql://${process.env.POSTGRES_USER || 'devops_user'}:${process.env.POSTGRES_PASSWORD || 'devops_secure_password'}@${process.env.POSTGRES_HOST || 'localhost'}:${process.env.POSTGRES_PORT || 5432}/${process.env.POSTGRES_DB || 'devops_control_center'}`;
  }
  
  const pool = new Pool({
    connectionString: databaseUrl,
  });

  try {
    console.log('🌱 Seeding database...');

    const seedFiles = [
      '01_roles.sql',
      '02_users.sql',
      '03_widget_types.sql',
      '04_approval_rules.sql',
      '05_service_health.sql',
    ];

    for (const file of seedFiles) {
      console.log(`  📄 Executing ${file}...`);
      const seedPath = path.join(__dirname, file);
      const seed = fs.readFileSync(seedPath, 'utf8');
      await pool.query(seed);
    }

    console.log('✅ Seeding completed successfully');
  } catch (error) {
    console.error('❌ Seeding failed:', error.message);
    process.exit(1);
  } finally {
    await pool.end();
  }
}

runSeeds();

