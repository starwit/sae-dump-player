# sae-dump-player
A simple webapp that allows to upload a .saedump file which is then played back.

## How to run
- Install dependencies
  ```sh
  apt install libglib2.0-0 libgl1 libturbojpeg0
  poetry install
  ```
- Run app
  ```sh
  poetry run fastapi run sae_dump_player/app.py
  ```
- Access application UI at `http://localhost:8000`(Swagger UI is at `http://localhost:8000/docs`)

## Example: Stop one running task using curl
```sh
id=$(curl -X 'GET' 'http://localhost:8000/tasks/' -H 'accept: application/json' | jq -r '[.tasks[] | select(.status == "running")][0].id') && \
curl -X 'DELETE' "http://localhost:8000/tasks/$id" -H 'accept: application' && \
unset id
```