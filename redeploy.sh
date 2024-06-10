#!/bin/bash
docker cp mk8dx-sqbot:/app/server_config.json .
docker build --tag mk8dx-sqbot .
docker stop mk8dx-sqbot
docker rm mk8dx-sqbot
docker run -d --name mk8dx-sqbot --restart unless-stopped mk8dx-sqbot