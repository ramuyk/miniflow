#!/bin/bash

set -e

python ../run.py db apply bootstrap sample
python ../run.py integrations apply namespace.hello-world
python ../run.py jobs apply namespace.job
python ../run.py inspect namespace.hello-world
python ../run.py inspect namespace.hello-shell

