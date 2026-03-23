#!/bin/bash

# supercronic setup
if [ ! -f supercronic ]; then
    echo "Downloading supercronic..."
    curl -L -o supercronic https://github.com/aptible/supercronic/releases/latest/download/supercronic-linux-amd64
    chmod +x supercronic
    echo "supercronic downloaded and made executable."
else
    echo "supercronic already exists, skipping download."
fi

# venv setup
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment .venv..."
    python3 -m venv .venv
    if [ $? -eq 0 ]; then
        echo "Virtual environment created successfully."
    else
        echo "Failed to create virtual environment." >&2
        exit 1
    fi
else
    echo "Virtual environment already exists, using it."
fi

echo "Activating the virtual environment..."
. .venv/bin/activate

echo "Installing dependencies via pip..."
pip install -r requirements.txt
if [ $? -eq 0 ]; then
    echo "Dependencies installed successfully!"
else
    echo "Failed to install dependencies." >&2
    exit 1
fi

echo ""
echo "To activate the virtual environment use: source .venv/bin/activate"
