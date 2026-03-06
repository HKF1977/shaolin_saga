#!/bin/bash

# Log directory
LOG_DIR="/home/shaolin_saga/logs"
STARTUP_LOG="$LOG_DIR/startup.log"
SHUTDOWN_LOG="$LOG_DIR/shutdown.log"

# Create logs directory if it doesn't exist
mkdir -p $LOG_DIR

# Function to start all services
start_services() {
    echo "$(date): === Starting Shaolin Saga services ===" >> $STARTUP_LOG
    
    # Function to start a Python script
    start_script() {
        echo "$(date): Starting $1..." >> $STARTUP_LOG
        nohup python3 $1 > $LOG_DIR/$(basename $1 .py).log 2>&1 &
        PID=$!
        echo "$(date): Started $1 with PID: $PID" >> $STARTUP_LOG
        echo $PID
    }
    
    # Start pump_main.py
    MONK_PID=$(start_script /home/shaolin_saga/app/pump_main.py)
    echo "pump_main.py started with PID: $MONK_PID"
    
    # Wait a few seconds to avoid any potential conflicts
    sleep 5
    
    # Start pump_trades.py
    WHALES_PID=$(start_script /home/shaolin_saga/app/pump_trades.py)
    echo "pump_trades.py started with PID: $WHALES_PID"
    
    echo "$(date): All services started successfully" >> $STARTUP_LOG
    echo "All Shaolin Saga services started successfully!"
}

# Function to stop all services
stop_services() {
    echo "$(date): === Stopping Shaolin Saga services ===" >> $SHUTDOWN_LOG
    
    # Find and kill pump_main.py
    MONK_PIDS=$(pgrep -f "python3 /home/shaolin_saga/app/pump_main.py")
    if [ -n "$MONK_PIDS" ]; then
        echo "$(date): Stopping pump_main.py (PIDs: $MONK_PIDS)" >> $SHUTDOWN_LOG
        echo "Stopping pump_main.py (PIDs: $MONK_PIDS)"
        kill $MONK_PIDS
        echo "$(date): pump_main.py stopped" >> $SHUTDOWN_LOG
    else
        echo "$(date): pump_main.py not running" >> $SHUTDOWN_LOG
        echo "pump_main.py not running"
    fi
    
    # Find and kill pump_trades.py
    WHALES_PIDS=$(pgrep -f "python3 /home/shaolin_saga/app/pump_trades.py")
    if [ -n "$WHALES_PIDS" ]; then
        echo "$(date): Stopping pump_trades.py (PIDs: $WHALES_PIDS)" >> $SHUTDOWN_LOG
        echo "Stopping pump_trades.py (PIDs: $WHALES_PIDS)"
        kill $WHALES_PIDS
        echo "$(date): pump_trades.py stopped" >> $SHUTDOWN_LOG
    else
        echo "$(date): pump_trades.py not running" >> $SHUTDOWN_LOG
        echo "pump_trades.py not running"
    fi
    
    echo "$(date): All services stopped" >> $SHUTDOWN_LOG
    echo "All Shaolin Saga services stopped!"
}

# Function to check status of services
check_status() {
    echo "=== Shaolin Saga Services Status ==="
    
    # Check pump_main.py
    MONK_PIDS=$(pgrep -f "python3 /home/shaolin_saga/app/pump_main.py")
    if [ -n "$MONK_PIDS" ]; then
        echo "pump_main.py: RUNNING (PIDs: $MONK_PIDS)"
    else
        echo "pump_main.py: NOT RUNNING"
    fi
    
    # Check pump_trades.py
    WHALES_PIDS=$(pgrep -f "python3 /home/shaolin_saga/app/pump_trades.py")
    if [ -n "$WHALES_PIDS" ]; then
        echo "pump_trades.py: RUNNING (PIDs: $WHALES_PIDS)"
    else
        echo "pump_trades.py: NOT RUNNING"
    fi
}

# Function to restart services
restart_services() {
    echo "Restarting Shaolin Saga services..."
    stop_services
    sleep 5  # Wait a bit for processes to fully terminate
    start_services
}

# Main script logic
case "$1" in
    start)
        start_services
        ;;
    stop)
        stop_services
        ;;
    restart)
        restart_services
        ;;
    status)
        check_status
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status}"
        exit 1
        ;;
esac

exit 0

