#!/bin/bash

docker build -t starwitorg/sae-dump-player:$(poetry version --short) .