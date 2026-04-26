// PM2 process manager config for Blinkit Hot Wheels Tracker
// 
// Start:   pm2 start ecosystem.config.js
// Stop:    pm2 stop hotwheels-tracker
// Restart: pm2 restart hotwheels-tracker
// Logs:    pm2 logs hotwheels-tracker
// Status:  pm2 status

module.exports = {
  apps: [
    {
      name: "hotwheels-tracker",
      script: "python",
      args: "tracker/main.py",
      cwd: "./",                     // Run from project root (hotwheels/)

      // Process management
      watch: false,                  // Don't watch files (avoid restarts on log writes)
      autorestart: true,
      restart_delay: 8000,           // 8s delay before restarting after crash
      max_restarts: 15,              // Stop trying after 15 crashes in a row
      min_uptime: "30s",             // Must run 30s to count as "stable"
      exp_backoff_restart_delay: 100,

      // Logging
      log_date_format: "YYYY-MM-DD HH:mm:ss",
      out_file: "./logs/pm2_out.log",
      error_file: "./logs/pm2_error.log",
      merge_logs: false,
      log_type: "raw",

      // Environment
      env: {
        NODE_ENV: "production",
        PYTHONUNBUFFERED: "1",       // Ensure Python output is not buffered
        PYTHONIOENCODING: "utf-8",
      },

      // Scheduling (optional — uncomment to only run during certain hours)
      // cron_restart: "0 6 * * *",  // Restart daily at 6 AM
    },
  ],
};
