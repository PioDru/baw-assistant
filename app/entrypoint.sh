#!/bin/sh
set -e

# Dane syntetyczne generujemy tylko przy pierwszym starcie (wolumen /data je przechowuje).
python -m baw_assistant.data_generator --if-missing

exec streamlit run streamlit_app.py \
    --server.address=0.0.0.0 \
    --server.port=8501 \
    --server.headless=true \
    --browser.gatherUsageStats=false
