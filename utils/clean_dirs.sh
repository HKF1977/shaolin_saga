#!/bin/bash

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
    #"/home/shaolin_saga/data/pump_data/pump_livestreams"
)

# Loop through each directory and perform cleanup
for dir in "${directories[@]}"; do
    echo "Processing $dir..."
    rm -rf "$dir" && mkdir "$dir"
    echo "Recreated $dir"
done

echo "All directories have been cleaned up and recreated."
