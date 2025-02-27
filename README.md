# sae-dump-player
A simple webapp that allows to upload a .saedump file which is then played back.

## Create and stop with curl
```sh
id=$(curl -X 'GET' 'http://localhost:8000/tasks/' -H 'accept: application/json' | jq -r '[.tasks[] | select(.status == "running")][0].id') && \
curl -X 'DELETE' "http://localhost:8000/tasks/$id" -H 'accept: application' && \
unset id
```