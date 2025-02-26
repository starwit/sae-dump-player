from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
import os
import uuid
import shutil
import time
import sqlite3
from sqlite3 import Connection, Row
import multiprocessing
from typing import Dict, List
from contextlib import contextmanager

app = FastAPI()

# Config
UPLOAD_DIR = "uploads"
DB_PATH = "file_storage.db"
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Database setup
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''
    CREATE TABLE IF NOT EXISTS files (
        id TEXT PRIMARY KEY,
        filename TEXT NOT NULL,
        content_type TEXT,
        path TEXT NOT NULL,
        upload_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    
    conn.execute('''
    CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        file_id TEXT NOT NULL,
        status TEXT NOT NULL,
        start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (file_id) REFERENCES files (id)
    )
    ''')
    conn.commit()
    conn.close()

# Initialize database on startup
@app.on_event("startup")
async def startup_event():
    init_db()

# Helper function to get a new database connection
def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# File endpoints
@app.post("/upload/")
async def upload_file(file: UploadFile = File(...)):
    # Connect to the database from within this request handler
    conn = get_db_connection()
    
    try:
        # Generate unique ID for the file
        file_id = str(uuid.uuid4())
        
        # Create path for saving the file
        file_path = os.path.join(UPLOAD_DIR, file_id)
        
        # Save the file
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        
        # Store metadata in SQLite
        conn.execute(
            "INSERT INTO files (id, filename, content_type, path) VALUES (?, ?, ?, ?)",
            (file_id, file.filename, file.content_type, file_path)
        )
        conn.commit()
        
        return {"file_id": file_id, "filename": file.filename}
    
    finally:
        conn.close()

@app.delete("/files/{file_id}")
async def delete_file(file_id: str):
    conn = get_db_connection()
    
    try:
        # Check if file exists
        cursor = conn.execute("SELECT path FROM files WHERE id = ?", (file_id,))
        file_record = cursor.fetchone()
        
        if not file_record:
            raise HTTPException(status_code=404, detail="File not found")
        
        # Get file path
        file_path = file_record["path"]
        
        # Delete file from filesystem
        if os.path.exists(file_path):
            os.remove(file_path)
        
        # Remove from database
        conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
        conn.commit()
        
        return {"status": "success", "message": f"File {file_id} deleted"}
    
    finally:
        conn.close()

@app.get("/files/")
async def list_files():
    conn = get_db_connection()
    
    try:
        cursor = conn.execute("SELECT id, filename, content_type, path, upload_time FROM files")
        files = [dict(row) for row in cursor.fetchall()]
        return {"files": files}
    
    finally:
        conn.close()

# Simulate processing a log file
def process_log_file(file_path: str, task_id: str, db_path: str):
    # Connect to the database from this process
    conn = sqlite3.connect(db_path)
    
    try:
        print(f"Starting processing task {task_id} for file: {file_path}")
        
        # Update status to running
        conn.execute(
            "UPDATE tasks SET status = ? WHERE id = ?",
            ("running", task_id)
        )
        conn.commit()
        
        try:
            # This would be your actual processing logic
            # For demonstration, we'll just simulate a CPU-intensive task
            for i in range(10):
                print(f"Processing {i*10}% complete for task {task_id}")
                time.sleep(1)  # Simulate work
            
            # Update status to completed
            conn.execute(
                "UPDATE tasks SET status = ? WHERE id = ?",
                ("completed", task_id)
            )
            conn.commit()
            print(f"Completed processing task {task_id}")
            
        except Exception as e:
            # Update status to failed
            conn.execute(
                "UPDATE tasks SET status = ? WHERE id = ?",
                (f"failed: {str(e)}", task_id)
            )
            conn.commit()
            print(f"Failed processing task {task_id}: {e}")
    
    finally:
        conn.close()

# Track running processes
active_processes: Dict[str, multiprocessing.Process] = {}

@app.post("/process/{file_id}")
async def start_processing(file_id: str):
    conn = get_db_connection()
    
    try:
        # Check if file exists
        cursor = conn.execute("SELECT path FROM files WHERE id = ?", (file_id,))
        file_record = cursor.fetchone()
        
        if not file_record:
            raise HTTPException(status_code=404, detail="File not found")
        
        file_path = file_record["path"]
        task_id = str(uuid.uuid4())
        
        # Create task record in database
        conn.execute(
            "INSERT INTO tasks (id, file_id, status) VALUES (?, ?, ?)",
            (task_id, file_id, "starting")
        )
        conn.commit()
        
        # Create and start a new process
        process = multiprocessing.Process(
            target=process_log_file,
            args=(file_path, task_id, DB_PATH)
        )
        process.start()
        
        # Store the process
        active_processes[task_id] = process
        
        return {"task_id": task_id, "status": "started", "file_id": file_id}
    
    finally:
        conn.close()

@app.get("/tasks/")
async def list_tasks():
    conn = get_db_connection()
    
    try:
        # Update status for completed processes
        for task_id, process in list(active_processes.items()):
            if not process.is_alive():
                process.join()
                active_processes.pop(task_id, None)
        
        # Get tasks from database
        cursor = conn.execute("SELECT id, file_id, status, start_time FROM tasks")
        tasks = [dict(row) for row in cursor.fetchall()]
        
        # Add live process information
        for task in tasks:
            if task["id"] in active_processes:
                task["has_active_process"] = True
            else:
                task["has_active_process"] = False
        
        return {"tasks": tasks}
    
    finally:
        conn.close()

@app.delete("/tasks/{task_id}")
async def cancel_task(task_id: str):
    conn = get_db_connection()
    
    try:
        # Check if task exists
        cursor = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,))
        task_record = cursor.fetchone()
        
        if not task_record:
            raise HTTPException(status_code=404, detail="Task not found")
        
        # Check if there's an active process
        if task_id in active_processes and active_processes[task_id].is_alive():
            active_processes[task_id].terminate()
            active_processes[task_id].join()
            active_processes.pop(task_id, None)
            
            # Update status in database
            conn.execute(
                "UPDATE tasks SET status = ? WHERE id = ?",
                ("cancelled", task_id)
            )
            conn.commit()
            
            return {"status": "cancelled", "task_id": task_id}
        else:
            return {"status": "not_running", "task_id": task_id, "current_status": task_record["status"]}
    
    finally:
        conn.close()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)