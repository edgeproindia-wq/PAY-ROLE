# Read automatically by gunicorn from the project folder (works with the existing Render start command).
# A payroll run for hundreds of employees can take longer than gunicorn's default 30 seconds, which would
# kill the request and show "Internal Server Error". Threads let other users work while a run is going.
workers = 1
worker_class = "gthread"
threads = 4
timeout = 120