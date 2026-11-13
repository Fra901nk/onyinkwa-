# Jadhum Fishing Equipments

## Run locally

Python 3.10 or later is required. The API uses Flask and Python's built-in SQLite support.

Install the Python dependency, set an admin password, and start the app from this folder in PowerShell:

```powershell
python -m pip install -r requirements.txt
$env:ADMIN_PASSWORD = "Choose-a-password-at-least-12-characters"
python app.py
```

Open the storefront at `http://localhost:3000` and the admin dashboard at `http://localhost:3000/admin`. The server binds to `127.0.0.1` by default and creates `data/quotations.sqlite` on first run. Keep the `data` folder private and backed up; it is excluded from Git.

Quotation requests are saved when a customer clicks an email, WhatsApp, or SMS request link. Each request gets a unique reference before the message composer opens. Customers still need to send the prepared message themselves.

## Admin and deployment

The admin password is supplied through the `ADMIN_PASSWORD` environment variable. If it is not set, local development uses a fallback default and displays a warning; production startup requires this variable. Use a unique password of at least 12 characters. Admin sessions expire after eight hours.

### Deploying online with Render

The included `render.yaml` configures a Python web service, Gunicorn, HTTPS-only admin cookies, and a persistent disk for the SQLite database. To deploy:

1. Push this project to a GitHub repository.
2. In Render, create a **Blueprint** from that repository and set the requested `ADMIN_PASSWORD` to a unique password of at least 12 characters.
3. Deploy. Render installs `requirements.txt` and starts the app using the configured Gunicorn command. Once deployed, use the Render URL for the storefront and append `/admin` for the dashboard.

The Blueprint uses a paid Render compute plan because persistent disks are not available on free web services. The disk keeps quotations and orders when the service restarts. The app is configured as a single service process so SQLite writes and in-memory admin sessions are consistent; administrators may need to sign in again after a restart or redeploy.

Vercel can run the Flask application, but its writable `/tmp` storage and per-instance in-memory sessions are not durable or shared. Do not use the included Vercel setup for real quotations and orders without switching to persistent external database and session storage. Opening `index.html` directly with `file://` does not provide API access.

At `/admin`, staff can:

- Add, edit, and archive products, including their descriptions, KES prices, and one of the bundled product photos.
- Add, rename, and delete empty categories.
- Search quotation requests, update their status, and record a confirmed request as an order.
- View customer records created from recorded orders, update order statuses, and review business activity and summary statistics.

Online checkout is not enabled. Orders are recorded by staff after the customer confirms by email, WhatsApp, or SMS. Product records are archived rather than permanently deleted so prior quotation history remains intact.
