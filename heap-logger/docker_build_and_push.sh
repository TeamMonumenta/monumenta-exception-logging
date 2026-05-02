#!/bin/bash

docker build . -t ghcr.io/teammonumenta/monumenta-automation/heap-logger && docker push ghcr.io/teammonumenta/monumenta-automation/heap-logger
