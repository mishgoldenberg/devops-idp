#!/usr/bin/env node

/**
 * Database seeder
 * Populates database with initial data (roles, users, widget types, etc.)
 */

const { Pool } = require('pg');
const fs = require('fs');
const path = require('path');

async function runSeeds() {
  const pool = new Pool({
    connectionString: process.env.DATABASE_URL || 'postgresql://devops_user:devops_secure_password@localhost:5432/devops_control_center',
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

