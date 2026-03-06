#!/bin/bash

# Scheduled maintenance script for pumpbot
# Runs every 4 hours to stop pumpbot and clean directories

# Set the working directory to the script's location
#SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#cd "$SCRIPT_DIR"

# Log file for maintenance activities
LOG_FILE="/home/shaolin_saga/logs/maintenance.log"

# Function to log messages with timestamp
log_message() {
    echo "$(date '+%Y-%m-%d %H:%M:%S'): $1" | tee -a "$LOG_FILE"
}

log_message "=== Starting scheduled maintenance ==="

# Stop pumpbot
log_message "Stopping Shaolin Saga..."
if [ -f "/home/shaolin_saga/utils/pumpbot.sh" ]; then
    /home/shaolin_saga/utils/pumpbot.sh stop
    if [ $? -eq 0 ]; then
        log_message "Shaolin Saga stopped successfully"
    else
        log_message "Warning: shaolin saga stop command returned non-zero exit code"
    fi
else
    log_message "Warning: pumpbot.sh not found in current directory"
fi

# Wait a moment for processes to fully stop
sleep 5

# Archive active_tokens and active_bonk_tokens before cleanup
log_message "Archiving active token directories..."
TIMESTAMP=$(date '+%Y%m%d')

if [ -d "/home/shaolin_saga/data/pump_data/active_tokens" ]; then
    cp -r "/home/shaolin_saga/data/pump_data/active_tokens" "/home/shaolin_saga/archived_tokens/archive_active_tokens_${TIMESTAMP}"
    if [ $? -eq 0 ]; then
        log_message "Successfully archived active_tokens to archive_active_tokens_${TIMESTAMP}"
    else
        log_message "Error: Failed to archive active_tokens"
    fi
else
    log_message "Warning: active_tokens directory not found"
fi

if [ -d "/home/shaolin_saga/data/bonk_data/active_bonk_tokens" ]; then
    cp -r "/home/shaolin_saga/data/bonk_data/active_bonk_tokens" "/home/shaolin_saga/archived_tokens/archive_bonk_active_tokens_${TIMESTAMP}"
    if [ $? -eq 0 ]; then
        log_message "Successfully archived active_bonk_tokens to archive_bonk_active_tokens_${TIMESTAMP}"
    else
        log_message "Error: Failed to archive active_bonk_tokens"
    fi
else
    log_message "Warning: active_bonk_tokens directory not found"
fi

# List of directories to clean up and recreate
directories=(
    "/home/shaolin_saga/data/pump_data/under15"
    "/home/shaolin_saga/data/pump_data/15percent"
    "/home/shaolin_saga/data/pump_data/35percent"
    "/home/shaolin_saga/data/pump_data/80percent"
    "/home/shaolin_saga/data/pump_data/95percent"
    "/home/shaolin_saga/data/pump_data/bondingComplete"
    "/home/shaolin_saga/data/pump_data/active_tokens"
    "/home/shaolin_saga/data/bonk_data/active_bonk_tokens"
    "/home/shaolin_saga/data/bonk_data/bonk_under80"
    "/home/shaolin_saga/data/bonk_data/bonk_80percent"
    "/home/shaolin_saga/data/bonk_data/bonk_95percent"
    "/home/shaolin_saga/data/bonk_data/bonk_bondingComplete"
)

# Clean and recreate directories
log_message "Starting directory cleanup..."
for dir in "${directories[@]}"; do
    log_message "Processing $dir..."
    if [ -d "$dir" ]; then
        rm -rf "$dir"
        log_message "Removed existing $dir"
    fi
    mkdir "$dir"
    if [ $? -eq 0 ]; then
        log_message "Successfully recreated $dir"
    else
        log_message "Error: Failed to create $dir"
    fi
done

log_message "Directory cleanup completed"

# Restart pumpbot
log_message "Restarting pumpbot..."
/home/shaolin_saga/utils/pumpbot.sh start
if [ $? -eq 0 ]; then
    log_message "Pumpbot restart initiated successfully"
else
    log_message "Warning: pumpbot start command returned non-zero exit code"
fi

log_message "=== Scheduled maintenance completed ==="
log_message ""
