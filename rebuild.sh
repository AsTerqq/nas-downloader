#!/bin/bash
cd /volume1/docker/nas-downloader
docker compose down
docker compose build
docker compose up -d
