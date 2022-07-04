#!/bin/bash
docker build --tag queuebot .
docker stop queuebot
docker rm queuebot
docker run -d --name queuebot --restart unless-stopped queuebot