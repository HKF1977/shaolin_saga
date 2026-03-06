#!/bin/bash

# Install cron job for scheduled maintenance script
# Runs scheduled_maintenance.sh every 3 hours

# Get the absolute path to the script directory

LOG_DIR="/home/shaolin_saga/logs"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MAINTENANCE_SCRIPT="$SCRIPT_DIR/scheduled_maintenance.sh"

# Check if the maintenance script exists
if [ ! -f "$MAINTENANCE_SCRIPT" ]; then
    echo "Error: scheduled_maintenance.sh not found at $MAINTENANCE_SCRIPT"
    exit 1
fi

# Make sure the maintenance script is executable
chmod +x "$MAINTENANCE_SCRIPT"

# Create the cron job entry
# Run every 9 hours starting at midnight (0:00, 9:00, 18:00)
CRON_ENTRY="0 */3 * * * $MAINTENANCE_SCRIPT >> $LOG_DIR/cron.log 2>&1"

# Check if cron job already exists
if crontab -l 2>/dev/null | grep -q "$MAINTENANCE_SCRIPT"; then
    echo "Cron job for scheduled maintenance already exists."
    echo "Current cron jobs:"
    crontab -l | grep "$MAINTENANCE_SCRIPT"
    echo ""
    read -p "Do you want to replace it? (y/n): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Installation cancelled."
        exit 0
    fi
    
    # Remove existing cron job
    crontab -l | grep -v "$MAINTENANCE_SCRIPT" | crontab -
    echo "Removed existing cron job."
fi

# Add the new cron job
(crontab -l 2>/dev/null; echo "$CRON_ENTRY") | crontab -

if [ $? -eq 0 ]; then
    echo "✅ Cron job installed successfully!"
    echo "Schedule: Every 9 hours (0:00, 9:00, 18:00)"
    echo "Script: $MAINTENANCE_SCRIPT"
    echo "Log: $LOG_DIR/cron.log"
    echo ""
    echo "Current cron jobs:"
    crontab -l
else
    echo "❌ Failed to install cron job"
    exit 1
fi

echo ""
echo "To manually run the maintenance script: $MAINTENANCE_SCRIPT"
echo "To view cron logs: tail -f $LOG_DIR/cron.log"
echo "To remove this cron job: crontab -e (then delete the line)"


