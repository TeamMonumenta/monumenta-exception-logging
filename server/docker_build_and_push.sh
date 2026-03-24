#!/bin/bash

docker build . -t docker.pkg.github.com/teammonumenta/monumenta-automation/monumenta-exception-logger && docker push docker.pkg.github.com/teammonumenta/monumenta-automation/monumenta-exception-logger
