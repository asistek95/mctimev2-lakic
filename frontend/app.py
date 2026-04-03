from flask import Flask, render_template, jsonify, request, Response, session, redirect, url_for
import os
import io
import csv
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
from functools import lru_cache
import hashlib
import json
import logging
from datetime import datetime, timedelta

# Fix Windows console encoding (charmap codec error with Unicode characters)
if sys.platform == 'win32':
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except Exception:
        pass

# Configure logging to handle UTF-8
logging.basicConfig(level=logging.INFO, format='[%(name)s] %(message)s',
                    handlers=[logging.StreamHandler(stream=sys.stdout)])
logger = logging.getLogger(__name__)

# Füge Projekt-Root zum Pfad hinzu
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

# Load environment variables from .env file if it exists
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))
except ImportError:
    print("python-dotenv not installed. Install with: pip install python-dotenv")
    print("Using environment variables directly.")

# Importiere Backend und Middleware
import middleware.core as middleware_core
from middleware.core import Middleware, get_middleware
from backend.api_handler import BackendService, McTimeAPI

app = Flask(__name__, template_folder='templates', static_folder='static')
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0
app.config['MCTIME_DATA_START'] = os.getenv('MCTIME_DATA_START', '2016-01-01')
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'dev-secret-key-change-in-production')

# ==================== HYBRID ARCHITEKTUR ====================
# Frontend → Middleware → Backend → McTime API
# Middleware: Authentifizierung, Rate Limiting, Fehlerbehandlung
# Backend: McTime API-Logik, Datenverarbeitung

API_KEY = os.getenv('MCTIME_API_KEY')

# ==================== PERFORMANCE CACHE ====================
# Cache für API-Daten (TTL: 5 Minuten)
_cache = {}
_cache_ttl = 300  # 5 Minuten

# ==================== REQUEST TRACKING ====================
# Globale Request-Statistiken für das Dashboard
_request_stats = {
    "total_requests": 0,
    "successful_requests": 0,
    "failed_requests": 0,
    "response_times": [],  # Letzte 100 Response-Zeiten
    "start_time": time.time()
}

def track_request(success: bool, response_time_ms: float = 0):
    """Trackt einen API-Request für Statistiken"""
    _request_stats["total_requests"] += 1
    if success:
        _request_stats["successful_requests"] += 1
    else:
        _request_stats["failed_requests"] += 1

    # Response-Zeit speichern (max 100)
    _request_stats["response_times"].append(response_time_ms)
    if len(_request_stats["response_times"]) > 100:
        _request_stats["response_times"] = _request_stats["response_times"][-100:]

def get_request_stats():
    """Gibt aktuelle Request-Statistiken zurück"""
    avg_time = 0
    if _request_stats["response_times"]:
        avg_time = sum(_request_stats["response_times"]) / len(_request_stats["response_times"])

    return {
        "total_requests": _request_stats["total_requests"],
        "successful_requests": _request_stats["successful_requests"],
        "failed_requests": _request_stats["failed_requests"],
        "avg_response_time_ms": round(avg_time, 1)
    }

def get_cache_key(prefix, **kwargs):
    """Erstellt einen eindeutigen Cache-Key"""
    data = json.dumps(kwargs, sort_keys=True)
    return f"{prefix}_{hashlib.md5(data.encode()).hexdigest()}"

def get_cached(key):
    """Holt Daten aus Cache wenn noch gültig"""
    if key in _cache:
        data, timestamp = _cache[key]
        if time.time() - timestamp < _cache_ttl:
            return data
        del _cache[key]
    return None

def set_cached(key, data):
    """Speichert Daten im Cache"""
    _cache[key] = (data, time.time())

def clear_cache():
    """Leert den Cache (inkl. Backend-Employees-Cache)"""
    global _cache
    _cache = {}
    if backend_service and hasattr(backend_service, 'mctime_api'):
        backend_service.mctime_api._employees_cache = None
    if middleware and hasattr(middleware, 'backend') and middleware.backend:
        middleware.backend.mctime_api._employees_cache = None


def _env_file_path() -> str:
    return os.path.join(os.path.dirname(__file__), '..', '.env')


def _mask_key(value: str) -> str:
    if not value:
        return "nicht gesetzt"
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}...{value[-4:]}"


def _upsert_env_values(values: dict) -> None:
    env_path = _env_file_path()

    existing_lines = []
    if os.path.exists(env_path):
        with open(env_path, 'r', encoding='utf-8') as f:
            existing_lines = f.readlines()

    updated_keys = set()
    new_lines = []

    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith('#') or '=' not in stripped:
            new_lines.append(line)
            continue

        key = stripped.split('=', 1)[0].strip()
        if key in values:
            new_lines.append(f"{key}={values[key]}\n")
            updated_keys.add(key)
        else:
            new_lines.append(line)

    for key, value in values.items():
        if key not in updated_keys:
            new_lines.append(f"{key}={value}\n")

    with open(env_path, 'w', encoding='utf-8') as f:
        f.writelines(new_lines)


def _reinitialize_services(new_api_key: str) -> dict:
    global API_KEY, backend_service, middleware

    API_KEY = new_api_key
    os.environ['MCTIME_API_KEY'] = new_api_key

    backend_error = None
    middleware_error = None

    try:
        backend_service = BackendService(API_KEY) if API_KEY else None
    except Exception as e:
        backend_service = None
        backend_error = str(e)

    try:
        middleware_core._middleware_instance = None
        middleware = get_middleware(API_KEY) if API_KEY else None
    except Exception as e:
        middleware = None
        middleware_error = str(e)

    clear_cache()

    return {
        "backend_ok": backend_service is not None,
        "middleware_ok": middleware is not None,
        "backend_error": backend_error,
        "middleware_error": middleware_error
    }


def _validate_mctime_api_key(api_key: str) -> dict:
    api = McTimeAPI(api_key)
    orgs = api.get_organizations()
    valid = isinstance(orgs, list) and len(orgs) > 0

    return {
        "valid": valid,
        "organizations": orgs,
        "organization_count": len(orgs) if isinstance(orgs, list) else 0
    }


def _is_dev_authenticated() -> bool:
    return bool(session.get('dev_authenticated'))


def _require_dev_login():
    if not _is_dev_authenticated():
        return redirect(url_for('developer_login'))
    return None


PRODUCTIVE_TIME_TYPES = {
    'work',
    'home_office',
    'travel',
    'clocking',
    'worked_rest_period',
    'flextime'
}

SICK_TIME_TYPES = {
    'sick',
    'medical_leave',
    'work_accident_sickness'
}


def _safe_float(value, default=0.0):
    try:
        if isinstance(value, str):
            return float(value.replace(',', '.'))
        return float(value)
    except (TypeError, ValueError, AttributeError):
        return default


def _to_hours(milliseconds):
    return round(_safe_float(milliseconds) / 3600000, 2)


def _normalize_personnel_number(value):
    if value is None:
        return ''
    return ''.join(ch for ch in str(value) if ch.isdigit())


def _build_full_name(first_name, last_name, fallback=''):
    full_name = f"{(first_name or '').strip()} {(last_name or '').strip()}".strip()
    return full_name or fallback or 'Unbekannt'


def _build_short_employee_name(first_name, last_name, fallback=''):
    first_name = (first_name or '').strip()
    last_name = (last_name or '').strip()

    if first_name and last_name:
        return f"{first_name[0]}. {last_name}"
    if last_name:
        return last_name
    if first_name:
        return first_name
    fallback = (fallback or '').strip()
    if not fallback:
        return 'Unbekannt'
    parts = fallback.split()
    if len(parts) >= 2:
        return f"{parts[0][0]}. {' '.join(parts[1:])}"
    return fallback


def _build_analytics_range(date_from, date_to):
    return (
        f"{date_from.strftime('%Y-%m-%d')}T00:00:00+02:00",
        f"{date_to.strftime('%Y-%m-%d')}T23:59:59+02:00"
    )


def _get_entry_date_key(entry):
    if entry.get('date_formatted'):
        return entry.get('date_formatted')
    raw_from = entry.get('from')
    if not raw_from:
        return None
    try:
        return datetime.fromisoformat(raw_from.replace('Z', '+00:00')).strftime('%d.%m.%y')
    except ValueError:
        return None


def _calculate_adherence_score(productive_hours, target_hours):
    if target_hours <= 0:
        return 0
    deviation_hours = productive_hours - target_hours
    score = 100 - (abs(deviation_hours) / target_hours * 100)
    return max(0, round(score))


def _get_target_start_minutes():
    """Gibt die Soll-Startzeit in Minuten seit Mitternacht zurück (aus .env TARGET_START_TIME)"""
    target = os.getenv('TARGET_START_TIME', '07:00').strip()
    try:
        parts = target.split(':')
        return int(parts[0]) * 60 + int(parts[1])
    except (ValueError, IndexError):
        return 7 * 60  # Default 07:00


def _calculate_punctuality_from_entries(time_entries):
    """
    Berechnet Pünktlichkeit aus Time-Entries.
    Misst den tatsächlichen Arbeitsbeginn vs. TARGET_START_TIME.
    Returns: dict mit total_days, on_time_days, late_days, total_late_minutes, avg_late_minutes, daily_details
    """
    target_minutes = _get_target_start_minutes()
    daily_starts = {}  # date_key -> frühester Start in Minuten

    for entry in time_entries:
        entry_type = (entry.get('type') or '').strip().lower()
        if entry_type not in PRODUCTIVE_TIME_TYPES:
            continue

        raw_from = entry.get('from')
        if not raw_from:
            continue

        try:
            dt = datetime.fromisoformat(raw_from.replace('Z', '+00:00'))
            date_key = dt.strftime('%Y-%m-%d')
            start_minutes = dt.hour * 60 + dt.minute

            # Frühesten Start pro Tag merken
            if date_key not in daily_starts or start_minutes < daily_starts[date_key]:
                daily_starts[date_key] = start_minutes
        except (ValueError, AttributeError):
            continue

    if not daily_starts:
        return {
            'total_days': 0, 'on_time_days': 0, 'late_days': 0,
            'total_late_minutes': 0, 'avg_late_minutes': 0, 'score': 100,
            'daily_details': []
        }

    total_days = len(daily_starts)
    late_days = 0
    total_late_minutes = 0
    daily_details = []

    for date_key, start_min in sorted(daily_starts.items()):
        diff = start_min - target_minutes  # positiv = zu spät, negativ = zu früh
        is_late = diff > 0
        if is_late:
            late_days += 1
            total_late_minutes += diff
        daily_details.append({
            'date': date_key,
            'start_minutes': start_min,
            'diff_minutes': diff,
            'late': is_late
        })

    on_time_days = total_days - late_days
    avg_late = round(total_late_minutes / total_days, 1) if total_days > 0 else 0
    score = round((on_time_days / total_days) * 100) if total_days > 0 else 100

    return {
        'total_days': total_days,
        'on_time_days': on_time_days,
        'late_days': late_days,
        'total_late_minutes': total_late_minutes,
        'avg_late_minutes': avg_late,
        'score': score,
        'daily_details': daily_details
    }


def _format_time_type(type_name):
    if not type_name:
        return 'Arbeitszeit'
    return str(type_name)

if not API_KEY:
    print("WARNING: MCTIME_API_KEY environment variable not set!")
    print("Please configure your .env file with MCTIME_API_KEY")

# Initialisiere Backend Service
try:
    backend_service = BackendService(API_KEY) if API_KEY else None
    if backend_service:
        print("[OK] Backend Service initialisiert")
except Exception as e:
    print(f"[FEHLER] Backend-Fehler: {e}")
    backend_service = None

# Initialisiere Middleware (für zusätzliche Features)
try:
    middleware = get_middleware(API_KEY) if API_KEY else None
    if middleware:
        print("[OK] Middleware initialisiert")
except Exception as e:
    print(f"[FEHLER] Middleware-Fehler: {e}")
    middleware = None

@app.route('/')
def home():
    """Hauptseite - Backend mit Middleware Fallback"""
    try:
        # Primär: Backend Service verwenden
        if backend_service:
            form_data = backend_service.get_form_data()
            companies = form_data.get('organizations', [])
            employees = form_data.get('employees', [])
            connection_status = form_data.get('status') == 'success'
        # Fallback: Middleware
        elif middleware:
            form_data = middleware.get_form_data()
            companies = form_data.get('organizations', [])
            employees = form_data.get('employees', [])
            connection_status = form_data.get('status') == 'success'
        else:
            raise Exception("Weder Backend noch Middleware initialisiert")
        
    except Exception as e:
        print(f"Fehler beim Laden der Daten: {e}")
        companies = []
        employees = []
        connection_status = False
    
    return render_template('index.html', 
                         companies=companies, 
                         employees=employees,
                         db_status=connection_status)

@app.route('/api-config')
def api_config():
    """Middleware-Konfigurationsseite"""
    return render_template('api_config.html')


@app.route('/developer/login', methods=['GET', 'POST'])
def developer_login():
    """Einfaches Login fuer das Developer-Panel"""
    configured_password = os.getenv('DEV_PANEL_PASSWORD', '').strip()

    if request.method == 'POST':
        submitted_password = (request.form.get('password') or '').strip()

        if not configured_password:
            return render_template('developer_connect.html',
                                 login_required=True,
                                 login_error='DEV_PANEL_PASSWORD ist nicht gesetzt. Bitte in .env konfigurieren.')

        if submitted_password == configured_password:
            session['dev_authenticated'] = True
            return redirect(url_for('developer_connect'))

        return render_template('developer_connect.html',
                             login_required=True,
                             login_error='Falsches Passwort.')

    return render_template('developer_connect.html', login_required=True)


@app.route('/developer/logout', methods=['POST'])
def developer_logout():
    session.pop('dev_authenticated', None)
    return redirect(url_for('developer_login'))


@app.route('/developer/login-api', methods=['POST'])
def developer_login_api():
    """Login fuer Unternehmensanbindung im Einstellungen-Tab"""
    configured_password = os.getenv('DEV_PANEL_PASSWORD', '').strip()
    payload = request.get_json(silent=True) or {}
    submitted_password = (payload.get('password') or '').strip()

    if not configured_password:
        return jsonify({"success": False, "message": "DEV_PANEL_PASSWORD ist nicht gesetzt."}), 400

    if submitted_password != configured_password:
        return jsonify({"success": False, "message": "Falsches Passwort."}), 401

    session['dev_authenticated'] = True
    return jsonify({"success": True, "message": "Login erfolgreich."})


@app.route('/developer/logout-api', methods=['POST'])
def developer_logout_api():
    """Logout fuer Unternehmensanbindung im Einstellungen-Tab"""
    session.pop('dev_authenticated', None)
    return jsonify({"success": True, "message": "Logout erfolgreich."})


@app.route('/developer/connect/status', methods=['GET'])
def developer_connect_status():
    """Statusdaten fuer Unternehmensanbindung im Einstellungen-Tab"""
    current_key = os.getenv('MCTIME_API_KEY', '')
    current_company_name = os.getenv('APP_COMPANY_NAME', 'McTime')
    current_org_filter = os.getenv('MCTIME_ORGANIZATION_FILTER', '')

    return jsonify({
        "authenticated": _is_dev_authenticated(),
        "current_key_masked": _mask_key(current_key),
        "current_company_name": current_company_name,
        "current_org_filter": current_org_filter
    })


@app.route('/developer/connect', methods=['GET'])
def developer_connect():
    """Developer-Seite fuer API-Key-Anbindung pro Kunde/Firma"""
    guard = _require_dev_login()
    if guard:
        return guard

    current_key = os.getenv('MCTIME_API_KEY', '')
    current_company_name = os.getenv('APP_COMPANY_NAME', 'McTime')
    current_org_filter = os.getenv('MCTIME_ORGANIZATION_FILTER', '')

    return render_template(
        'developer_connect.html',
        login_required=False,
        current_key_masked=_mask_key(current_key),
        current_company_name=current_company_name,
        current_org_filter=current_org_filter
    )


@app.route('/developer/connect/test', methods=['POST'])
def developer_connect_test():
    """Prueft den uebergebenen API-Key gegen McTime /organizations"""
    if not _is_dev_authenticated():
        return jsonify({"success": False, "message": "Nicht eingeloggt"}), 401

    payload = request.get_json(silent=True) or {}
    api_key = (payload.get('api_key') or '').strip()

    if not api_key:
        return jsonify({"success": False, "message": "API-Key fehlt"}), 400

    try:
        validation = _validate_mctime_api_key(api_key)
        if not validation['valid']:
            return jsonify({
                "success": False,
                "message": "API-Key konnte nicht validiert werden oder liefert keine Organizations.",
                "organization_count": validation['organization_count']
            }), 400

        return jsonify({
            "success": True,
            "message": "API-Key ist gueltig.",
            "organization_count": validation['organization_count'],
            "organizations": validation['organizations']
        })
    except Exception as e:
        return jsonify({"success": False, "message": f"Validierung fehlgeschlagen: {str(e)}"}), 500


@app.route('/developer/connect/save', methods=['POST'])
def developer_connect_save():
    """Speichert API-Key/Firmennamen in .env und startet Services neu"""
    if not _is_dev_authenticated():
        return jsonify({"success": False, "message": "Nicht eingeloggt"}), 401

    payload = request.get_json(silent=True) or {}
    api_key = (payload.get('api_key') or '').strip()
    company_name = (payload.get('company_name') or '').strip() or 'McTime'
    organization_filter = (payload.get('organization_filter') or '').strip()

    if not api_key:
        return jsonify({"success": False, "message": "API-Key fehlt"}), 400

    try:
        validation = _validate_mctime_api_key(api_key)
        if not validation['valid']:
            return jsonify({
                "success": False,
                "message": "API-Key ist ungueltig oder liefert keine Organisationsdaten."
            }), 400

        _upsert_env_values({
            'MCTIME_API_KEY': api_key,
            'APP_COMPANY_NAME': company_name,
            'MCTIME_ORGANIZATION_FILTER': organization_filter
        })

        os.environ['APP_COMPANY_NAME'] = company_name
        os.environ['MCTIME_ORGANIZATION_FILTER'] = organization_filter
        reinit_status = _reinitialize_services(api_key)

        return jsonify({
            "success": True,
            "message": "API-Key erfolgreich gespeichert und Dienste neu geladen.",
            "organization_count": validation['organization_count'],
            "organizations": validation['organizations'],
            "service_status": reinit_status
        })
    except Exception as e:
        return jsonify({"success": False, "message": f"Speichern fehlgeschlagen: {str(e)}"}), 500

@app.route('/charts')
def charts():
    """Charts und Statistiken Seite"""
    return render_template('charts.html')

@app.route('/page_1')
def page_1():
    return render_template('321.html')

@app.route('/api/status')
def system_status():
    """Gibt den erweiterten Status von Backend und Middleware zurück"""

    # Middleware-Features prüfen
    middleware_features = {
        "authentication": {
            "name": "Authentifizierung",
            "status": False,
            "description": "API-Key basierte Authentifizierung"
        },
        "rate_limiting": {
            "name": "Rate Limiting",
            "status": False,
            "description": "Anfragenlimitierung zum Schutz der API"
        },
        "email_service": {
            "name": "E-Mail Service",
            "status": False,
            "description": "SMTP-basierter E-Mail-Versand"
        },
        "data_transformation": {
            "name": "Datentransformation",
            "status": False,
            "description": "JSON → CSV Konvertierung"
        },
        "multi_employee": {
            "name": "Multi-Mitarbeiter",
            "status": False,
            "description": "Mehrere Mitarbeiter gleichzeitig verarbeiten"
        },
        "csv_export": {
            "name": "CSV Export",
            "status": False,
            "description": "WorkExpert-kompatibles CSV-Format"
        },
        "caching": {
            "name": "Caching",
            "status": False,
            "description": "Performance-Cache für API-Daten"
        },
        "api_v2": {
            "name": "API v2 Kompatibilität",
            "status": False,
            "description": "McTime API Version 2 Support"
        }
    }

    # Health-Statistiken - nutze App-Level Tracking
    app_stats = get_request_stats()
    health_stats = {
        "uptime": "N/A",
        "requests_total": app_stats["total_requests"],
        "requests_success": app_stats["successful_requests"],
        "requests_failed": app_stats["failed_requests"],
        "avg_response_time": app_stats["avg_response_time_ms"],
        "cache_entries": len(_cache),
        "cache_hit_rate": "N/A"
    }

    if middleware:
        # Authentication ist aktiv wenn API-Key gesetzt
        middleware_features["authentication"]["status"] = bool(middleware.api_key)

        # Rate Limiting (Request-Handler prüfen)
        if hasattr(middleware, 'request_handler') and middleware.request_handler:
            middleware_features["rate_limiting"]["status"] = True

        # E-Mail Service aktiv wenn MailManager existiert
        if hasattr(middleware, 'mail') and middleware.mail:
            middleware_features["email_service"]["status"] = True

        # Datentransformation (immer aktiv wenn Middleware)
        middleware_features["data_transformation"]["status"] = True

        # Multi-Mitarbeiter (prüfe ob Methode existiert)
        if hasattr(middleware, 'send_multi_employee_report'):
            middleware_features["multi_employee"]["status"] = True

        # CSV Export
        if hasattr(middleware, 'convert_to_csv'):
            middleware_features["csv_export"]["status"] = True

        # Caching (aktiv wenn Cache existiert)
        middleware_features["caching"]["status"] = len(_cache) >= 0  # Immer True

        # API v2 Kompatibilität
        if hasattr(middleware, 'request_handler') and middleware.request_handler:
            if hasattr(middleware.request_handler, 'base_url'):
                middleware_features["api_v2"]["status"] = "v2" in middleware.request_handler.base_url

    status = {
        "backend": {
            "available": backend_service is not None,
            "api_configured": bool(API_KEY)
        },
        "middleware": {
            "available": middleware is not None,
            "status": middleware.get_connection_status() if middleware else None,
            "features": middleware_features
        },
        "health": health_stats
    }
    return jsonify(status)

@app.route('/api/cache/clear', methods=['POST'])
def clear_api_cache():
    """Leert den Anwendungs-Cache"""
    global _cache
    print("=== CACHE CLEAR CALLED ===")
    _cache = {}
    logger.info("Cache wurde manuell geleert.")
    return jsonify({"status": "success", "message": "Cache erfolgreich geleert."})

@app.route('/api/middleware/ping')
def ping_middleware():
    """Testet die Verbindung zur Middleware/McTime API"""
    if not middleware:
        return jsonify({
            "status": "error",
            "message": "Middleware nicht initialisiert"
        })
    return jsonify(middleware.health_check())

@app.route('/api/middleware/stats')
def middleware_stats():
    """Gibt Middleware-Statistiken zurück"""
    if not middleware:
        return jsonify({"error": "Middleware nicht initialisiert"})
    return jsonify(middleware.request_handler.get_stats())

@app.route('/api/stats/reset', methods=['POST'])
def reset_stats():
    """Setzt die Request-Statistiken und den Cache zurück"""
    global _request_stats
    print("=== RESET STATS CALLED ===")
    
    # Reset Request-Statistiken
    _request_stats = {
        "total_requests": 0,
        "successful_requests": 0,
        "failed_requests": 0,
        "response_times": [],
        "start_time": time.time()
    }
    
    print(f"[OK] Stats nach Reset: {_request_stats}")
    
    return jsonify({
        "status": "success", 
        "message": "Statistiken erfolgreich zurückgesetzt",
        "stats": _request_stats
    })

@app.route('/api/load-data', methods=['POST'])
def load_data():
    """
    Endpoint für McTime API Daten-Laden - BACKEND PRIMÄR
    Expected JSON payload:
    {
        "firma": "organization_id",
        "mitarbeiter": "employee_id",
        "von": "dd.mm.yyyy",
        "bis": "dd.mm.yyyy"
    }
    """
    start_time = time.time()
    try:
        form_data = request.get_json()
        print("=== HYBRID API CALL ===")
        print(f"Received form_data: {form_data}")

        if not form_data:
            print("ERROR: No JSON data provided")
            track_request(False, 0)
            return jsonify({
                "status": "error",
                "message": "No JSON data provided"
            }), 400

        # Primär: Backend Service verwenden
        if backend_service:
            print("Using Backend Service...")
            result = backend_service.process_form_request(form_data)
        # Fallback: Middleware
        elif middleware:
            print("Fallback: Using Middleware...")
            result = middleware.process_form_request(form_data)
        else:
            track_request(False, (time.time() - start_time) * 1000)
            return jsonify({
                "status": "error",
                "message": "Weder Backend noch Middleware verfügbar"
            }), 500

        response_time = (time.time() - start_time) * 1000
        print(f"Result: {result}")

        if result.get("status") == "error":
            print(f"Service returned error: {result.get('message')}")
            track_request(False, response_time)
            return jsonify(result), 400

        print(f"Success! Returning {len(result.get('data', {}).get('timeEntries', []))} time entries")
        track_request(True, response_time)
        return jsonify(result)

    except Exception as e:
        track_request(False, (time.time() - start_time) * 1000)
        return jsonify({
            "status": "error",
            "message": f"Failed to process request: {str(e)}"
        }), 500

@app.route('/api/data')
def get_data():
    """Holt gefilterte Daten - Backend primär, Middleware Fallback"""
    company = request.args.get('company')
    employee = request.args.get('employee')
    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to')
    
    if not all([employee, date_from, date_to]):
        return jsonify({"error": "Fehlende Parameter: employee, date_from, date_to"}), 400
    
    try:
        # Primär: Backend verwenden
        if backend_service:
            # Konvertiere Format für Backend
            date_from = backend_service._convert_date_format(date_from) if '.' in date_from else date_from
            date_to = backend_service._convert_date_format(date_to) if '.' in date_to else date_to
            
            data = backend_service.mctime_api.get_time_entries(
                employee_id=employee,
                date_from=date_from,
                date_to=date_to,
                organization_id=company
            )
        # Fallback: Middleware
        elif middleware:
            data = middleware.get_time_entries(
                employee_id=employee,
                date_from=date_from,
                date_to=date_to,
                organization_id=company
            )
        else:
            return jsonify({"error": "Kein Service verfügbar"}), 500
        
        return jsonify(data)
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/send-email', methods=['POST'])
def send_email():
    """Send time tracking data via email - HYBRID APPROACH"""
    try:
        # Get form data (JSON oder Form)
        if request.is_json:
            data = request.get_json()
        else:
            data = request.form.to_dict()
        
        employee_id = data.get('employee_id')
        employee_name = data.get('employee_name')
        date_from = data.get('date_from')
        date_to = data.get('date_to')
        
        if not all([employee_id, date_from, date_to]):
            return jsonify({
                'status': 'error',
                'message': 'Fehlende Parameter: employee_id, date_from, date_to erforderlich'
            })
        
        # Erweiterte Middleware-Features verfügbar?
        if middleware and hasattr(middleware, 'send_time_report_email'):
            # Neue optionale Parameter
            custom_email = data.get('email_to')
            custom_subject = data.get('email_subject')
            attach_csv = data.get('attach_csv', 'true').lower() == 'true'
            
            # Konvertiere Datum
            date_from = middleware._normalize_date(date_from)
            date_to = middleware._normalize_date(date_to)
            
            # Nutze erweiterte Middleware-Features
            result = middleware.send_time_report_email(
                employee_id=employee_id,
                employee_name=employee_name or "Mitarbeiter",
                date_from=date_from,
                date_to=date_to,
                custom_email=custom_email,
                custom_subject=custom_subject,
                attach_csv=attach_csv
            )
        
        elif backend_service:
            # Basic E-Mail über Backend (legacy)
            # Konvertiere Datum für Backend
            if '.' in date_from:
                date_from = backend_service._convert_date_format(date_from)
            if '.' in date_to:
                date_to = backend_service._convert_date_format(date_to)
            
            # Hole E-Mail und Zeitdaten über Backend
            employee_email = backend_service.mctime_api.get_user_email_by_id(employee_id)
            if not employee_email:
                return jsonify({
                    'status': 'error',
                    'message': f'Keine E-Mail-Adresse für {employee_name} gefunden'
                })
            
            time_entries = backend_service.mctime_api.get_time_entries(
                employee_id, date_from, date_to
            )
            
            if not time_entries:
                return jsonify({
                    'status': 'error',
                    'message': 'Keine Zeiteinträge gefunden'
                })
            
            # Basic E-Mail-Versand (ohne erweiterte Features)
            result = {
                'status': 'success',
                'message': 'Daten verfügbar - erweiterte E-Mail-Features benötigen Middleware',
                'email': employee_email,
                'entries_count': len(time_entries)
            }
        
        else:
            return jsonify({
                'status': 'error',
                'message': 'Weder Backend noch Middleware verfügbar'
            }), 500
        
        return jsonify(result)
            
    except Exception as e:
        print(f"Error in send_email: {e}")
        return jsonify({
            'status': 'error',
            'message': f'Server-Fehler: {str(e)}'
        })


# ==================== ZUSÄTZLICHE MIDDLEWARE-ENDPOINTS ====================

@app.route('/api/employees')
def get_employees():
    """Holt Mitarbeiterliste über Middleware"""
    start_time = time.time()
    if not middleware:
        track_request(False, 0)
        return jsonify({"error": "Middleware nicht initialisiert"}), 500

    try:
        org_id = request.args.get('organization_id')
        org_name = request.args.get('organization_name')
        employees = middleware.get_employees(org_id, org_name)

        response_time = (time.time() - start_time) * 1000
        print(f"API /api/employees - org_id: {org_id}, org_name: {org_name}, gefunden: {len(employees)} Mitarbeiter")
        track_request(True, response_time)
        return jsonify(employees)
    except Exception as e:
        track_request(False, (time.time() - start_time) * 1000)
        return jsonify({"error": str(e)}), 500


@app.route('/api/organizations')
def get_organizations():
    """Holt Organisationsliste über Middleware"""
    start_time = time.time()
    if not middleware:
        track_request(False, 0)
        return jsonify({"error": "Middleware nicht initialisiert"}), 500

    try:
        organizations = middleware.get_organizations()
        track_request(True, (time.time() - start_time) * 1000)
        return jsonify(organizations)
    except Exception as e:
        track_request(False, (time.time() - start_time) * 1000)
        return jsonify({"error": str(e)}), 500


@app.route('/api/test', methods=['GET'])
def api_test():
    """Einfacher Test-Endpoint"""
    return jsonify({"status": "ok", "message": "API works!"})

@app.route('/api/chart-stats')
def get_chart_stats():
    """Gibt aggregierte Chart-Daten basierend auf Filtertyp zurück"""
    filter_type = request.args.get('filter', 'month')
    month_str = request.args.get('month')       # Format: YYYY-MM (z.B. "2025-12")
    from_date_str = request.args.get('from')     # Format: YYYY-MM-DD
    to_date_str = request.args.get('to')         # Format: YYYY-MM-DD

    logger.info(f"Chart-Stats: filter={filter_type}, month={month_str}, from={from_date_str}, to={to_date_str}")

    try:
        # Bestimme den Datumsbereich
        if filter_type == 'custom' and from_date_str and to_date_str:
            date_from = datetime.strptime(from_date_str, '%Y-%m-%d')
            date_to = datetime.strptime(to_date_str, '%Y-%m-%d')
        elif filter_type == 'month' and month_str:
            # month_str = "2025-12" -> Anfang und Ende des Monats
            try:
                year, month = map(int, month_str.split('-'))
                date_from = datetime(year, month, 1)
                if month == 12:
                    date_to = datetime(year, 12, 31)
                else:
                    date_to = datetime(year, month + 1, 1) - timedelta(days=1)
                logger.info(f"Month filter: {month_str} -> {date_from.strftime('%Y-%m-%d')} to {date_to.strftime('%Y-%m-%d')}")
            except ValueError:
                logger.error(f"Invalid month format: {month_str}")
                # Fallback to current month
                now = datetime.now()
                date_from = now.replace(day=1)
                next_month = (now.replace(day=28) + timedelta(days=4)).replace(day=1)
                date_to = next_month - timedelta(days=1)
        elif month_str and not filter_type:
            # Handle case where month is sent without explicit filter_type
            try:
                year, month = map(int, month_str.split('-'))
                date_from = datetime(year, month, 1)
                if month == 12:
                    date_to = datetime(year, 12, 31)
                else:
                    date_to = datetime(year, month + 1, 1) - timedelta(days=1)
                filter_type = 'month'  # Set filter_type for proper labeling
                logger.info(f"Auto-detected month filter: {month_str} -> {date_from.strftime('%Y-%m-%d')} to {date_to.strftime('%Y-%m-%d')}")
            except ValueError:
                logger.error(f"Invalid month format in auto-detect: {month_str}")
                # Fallback to current month
                now = datetime.now()
                date_from = now.replace(day=1)
                next_month = (now.replace(day=28) + timedelta(days=4)).replace(day=1)
                date_to = next_month - timedelta(days=1)
        else:
            now = datetime.now()
            if filter_type == 'year':
                date_from = now.replace(month=1, day=1)
                date_to = now.replace(month=12, day=31)
            elif filter_type == 'alltime':
                data_start_str = os.getenv('MCTIME_DATA_START', '2016-01-01')
                try:
                    date_from = datetime.strptime(data_start_str, '%Y-%m-%d')
                except ValueError:
                    date_from = datetime(2016, 1, 1)
                date_to = now
            elif filter_type == 'quarter':
                current_quarter = (now.month - 1) // 3 + 1
                first_month = 3 * current_quarter - 2
                last_month = 3 * current_quarter
                date_from = now.replace(month=first_month, day=1)
                if last_month == 12:
                    next_month = datetime(now.year + 1, 1, 1)
                else:
                    next_month = now.replace(month=last_month + 1, day=1)
                date_to = next_month - timedelta(days=1)
            else:  # default to 'month' (aktueller Monat)
                date_from = now.replace(day=1)
                next_month = (now.replace(day=28) + timedelta(days=4)).replace(day=1)
                date_to = next_month - timedelta(days=1)

        date_from_api = date_from.strftime('%d.%m.%Y')
        date_to_api = date_to.strftime('%d.%m.%Y')
        analytics_from_api, analytics_to_api = _build_analytics_range(date_from, date_to)

        logger.info(f"Date range: {date_from_api} to {date_to_api}")

        if not middleware:
            raise Exception("Middleware nicht verfügbar")

        # Versuche Daten über Backend zu aggregieren
        try:
            # Hole alle Mitarbeiter
            employees = middleware.get_employees()

            daily_hours = {}
            employee_hours = {}
            project_hours = {}
            weekday_hours = [0, 0, 0, 0, 0, 0, 0]  # Mo-So
            total_hours = 0
            total_entries = 0
            employee_names_full = []
            punctuality_details = []
            sickday_details = []
            total_target_hours = 0.0
            total_productive_hours = 0.0

            # Sammle Daten von allen Mitarbeitern PARALLEL (statt sequentiell)
            def fetch_employee_entries(emp):
                emp_id = emp.get('id') or emp.get('employee_id')
                first_name = emp.get('firstName') or emp.get('first_name') or ''
                last_name = emp.get('lastName') or emp.get('last_name') or ''
                emp_name = emp.get('name') or emp.get('employee_name') or _build_full_name(first_name, last_name, emp_id)
                if not emp_id:
                    return None
                try:
                    time_entries = middleware.get_time_entries(
                        employee_id=emp_id,
                        date_from=date_from_api,
                        date_to=date_to_api
                    )
                    analytics = {}
                    if backend_service and backend_service.mctime_api:
                        analytics = backend_service.mctime_api.get_analytics(
                            analytics_from=analytics_from_api,
                            analytics_to=analytics_to_api,
                            user_ids=[emp_id]
                        )
                    return {
                        'employee_id': emp_id,
                        'full_name': emp_name,
                        'first_name': first_name,
                        'last_name': last_name,
                        'personnel_number': _normalize_personnel_number(emp.get('personnel_number')),
                        'time_entries': time_entries,
                        'analytics': analytics
                    }
                except Exception as e:
                    logger.warning(f"Fehler bei Mitarbeiter {emp_id}: {e}")
                    return None

            with ThreadPoolExecutor(max_workers=8) as executor:
                results = list(executor.map(fetch_employee_entries, employees))

            for result in results:
                if result is None:
                    continue
                emp_id = result['employee_id']
                emp_name = result['full_name']
                first_name = result['first_name']
                last_name = result['last_name']
                short_name = _build_short_employee_name(first_name, last_name, emp_name)
                time_entries = result['time_entries']
                analytics_data = result.get('analytics') or {}

                totals = analytics_data.get('totals', {}) if isinstance(analytics_data, dict) else {}
                productive_hours_analytics = _to_hours((totals.get('productive') or {}).get('total'))
                target_hours_analytics = _to_hours((totals.get('targetTimes') or {}).get('total'))
                deviation_hours = round(productive_hours_analytics - target_hours_analytics, 2)

                if target_hours_analytics > 0 or productive_hours_analytics > 0:
                    total_target_hours += target_hours_analytics
                    total_productive_hours += productive_hours_analytics

                # Auslastung: Ist vs. Soll (wie viel % der Soll-Stunden erreicht)
                if target_hours_analytics > 0:
                    utilization_score = round((productive_hours_analytics / target_hours_analytics) * 100)
                elif productive_hours_analytics > 0:
                    utilization_score = 100
                else:
                    utilization_score = 0

                if target_hours_analytics > 0 or productive_hours_analytics > 0:
                    punctuality_details.append({
                        'name': emp_name,
                        'short_name': short_name,
                        'score': utilization_score,
                        'target_hours': target_hours_analytics,
                        'productive_hours': productive_hours_analytics,
                        'deviation_hours': deviation_hours,
                    })

                if time_entries:
                    total_entries += len(time_entries)
                    emp_total = 0
                    sick_dates = set()
                    for entry in time_entries:
                        entry_type = (entry.get('type') or '').strip().lower()
                        date_key = _get_entry_date_key(entry)

                        if entry_type in SICK_TIME_TYPES and date_key:
                            sick_dates.add(date_key)

                        if entry_type not in PRODUCTIVE_TIME_TYPES:
                            continue

                        hours = _safe_float(entry.get('actual_work_hours', 0), 0)

                        emp_total += hours

                        if not date_key:
                            date_key = 'unknown'

                        if date_key not in daily_hours:
                            daily_hours[date_key] = 0
                        daily_hours[date_key] += hours

                        # Wochentag-Aggregation
                        try:
                            if date_key and date_key != 'unknown':
                                parts = date_key.split('.')
                                if len(parts) == 3:
                                    day_val = int(parts[0])
                                    month_val = int(parts[1])
                                    year_val = int(parts[2])
                                    if year_val < 100:
                                        year_val += 2000
                                    dt = datetime(year_val, month_val, day_val)
                                    weekday_idx = dt.weekday()
                                    weekday_hours[weekday_idx] += hours
                        except (ValueError, IndexError):
                            pass

                        # Projekt-Aggregation
                        project = entry.get('project', '') or 'Sonstiges'
                        if project not in project_hours:
                            project_hours[project] = 0
                        project_hours[project] += hours

                    if sick_dates:
                        sickday_details.append({
                            'name': emp_name,
                            'short_name': short_name,
                            'days': len(sick_dates)
                        })

                    if emp_total > 0:
                        employee_hours[emp_name or emp_id] = emp_total
                        employee_names_full.append(emp_name or emp_id)
                        total_hours += emp_total

            # Sortierte Mitarbeiter-Daten (Top 10)
            sorted_employees = sorted(employee_hours.items(), key=lambda x: x[1], reverse=True)
            top_employees = sorted_employees[:10]

            # Sortierte Projekt-Daten
            sorted_projects = sorted(project_hours.items(), key=lambda x: x[1], reverse=True)

            # Monatstrend - tägliche Stunden nach Datum sortiert
            sorted_daily = sorted(
                [(key, value) for key, value in daily_hours.items() if key != 'unknown'],
                key=lambda item: datetime.strptime(item[0], '%d.%m.%y')
            )

            # Berechne KPIs
            active_employees = len([e for e in sorted_employees if e[1] > 0])
            avg_hours = round(total_hours / active_employees, 1) if active_employees > 0 else 0
            total_sick_days = sum(item['days'] for item in sickday_details)
            overall_deviation_hours = round(total_productive_hours - total_target_hours, 2)

            # Auslastung: Gesamt-Score (Ist/Soll in %)
            utilization_score = round((total_productive_hours / total_target_hours) * 100) if total_target_hours > 0 else 0

            punctuality_details = sorted(
                punctuality_details,
                key=lambda item: item['score'],
                reverse=True
            )
            sickday_details = sorted(sickday_details, key=lambda item: item['days'], reverse=True)[:3]

            # Wochentag-Stunden runden
            weekday_hours = [round(h, 1) for h in weekday_hours]

            # Filter-Label für bessere Anzeige
            if filter_type == 'month' and month_str:
                # Konvertiere "2025-12" zu "Dezember 2025"
                try:
                    year, month = map(int, month_str.split('-'))
                    german_months = ['Januar', 'Februar', 'März', 'April', 'Mai', 'Juni',
                                   'Juli', 'August', 'September', 'Oktober', 'November', 'Dezember']
                    filter_label = f"{german_months[month-1]} {year}"
                except (ValueError, IndexError):
                    filter_label = f"{date_from.strftime('%d.%m.%Y')} - {date_to.strftime('%d.%m.%Y')}"
            else:
                filter_label = f"{date_from.strftime('%d.%m.%Y')} - {date_to.strftime('%d.%m.%Y')}"

            response_data = {
                "status": "success",
                "filter": filter_type,
                "filter_label": filter_label,
                # KPI-Daten im erwarteten Format
                "kpi": {
                    "total_hours": round(total_hours, 2),
                    "employee_count": active_employees,
                    "entry_count": total_entries,
                    "avg_hours_per_employee": avg_hours,
                    "utilization_score": utilization_score,
                    "punctuality_diff_hours": overall_deviation_hours,
                    "sick_days_total": total_sick_days
                },
                # Wochentag-Daten (Mo-So)
                "weekday_data": {
                    "values": weekday_hours
                },
                # Projekt-Daten
                "project_data": {
                    "labels": [p[0] for p in sorted_projects[:5]],
                    "values": [round(p[1], 2) for p in sorted_projects[:5]]
                },
                # Monatstrend (tägliche Daten)
                "monthly_data": {
                    "labels": [d[0] for d in sorted_daily],
                    "values": [round(d[1], 2) for d in sorted_daily]
                },
                # Mitarbeiter-Ranking
                "employee_data": {
                    "labels": [
                        _build_short_employee_name(
                            next((emp.get('firstName', '') for emp in employees if (emp.get('name') or '') == e[0]), ''),
                            next((emp.get('lastName', '') for emp in employees if (emp.get('name') or '') == e[0]), ''),
                            e[0]
                        )
                        for e in top_employees
                    ],
                    "values": [round(e[1], 2) for e in top_employees],
                    "full_names": [e[0] for e in top_employees],
                    "total_count": len(sorted_employees),
                    "showing": len(top_employees)
                },
                "utilization_data": {
                    "score": utilization_score,
                    "target_hours": round(total_target_hours, 2),
                    "productive_hours": round(total_productive_hours, 2),
                    "difference_hours": overall_deviation_hours,
                    "details": punctuality_details
                },
                "sickdays_data": {
                    "total": total_sick_days,
                    "details": sickday_details
                }
            }

            logger.info(f"Chart data generated: {len(sorted_daily)} days, {active_employees} employees, {total_entries} entries")
            return jsonify(response_data)

        except Exception as e:
            logger.error(f"Fehler bei Datenaggregation: {e}", exc_info=True)
            # Rückfallwert: Leere Daten mit korrektem Format
            return jsonify({
                "status": "success",
                "filter": filter_type,
                "filter_label": f"{date_from.strftime('%d.%m.%Y')} - {date_to.strftime('%d.%m.%Y')}",
                "kpi": {
                    "total_hours": 0,
                    "employee_count": 0,
                    "entry_count": 0,
                    "avg_hours_per_employee": 0,
                    "utilization_score": 0,
                    "punctuality_diff_hours": 0,
                    "sick_days_total": 0
                },
                "weekday_data": {"values": [0, 0, 0, 0, 0, 0, 0]},
                "project_data": {"labels": [], "values": []},
                "monthly_data": {"labels": [], "values": []},
                "employee_data": {"labels": [], "values": [], "full_names": [], "total_count": 0, "showing": 0},
                "utilization_data": {"score": 0, "target_hours": 0, "productive_hours": 0, "difference_hours": 0, "details": []},
                "sickdays_data": {"total": 0, "details": []}
            })

    except Exception as e:
        logger.error(f"Fehler in get_chart_stats: {e}", exc_info=True)
        return jsonify({
            "status": "error",
            "message": str(e),
            "error": str(e)
        }), 500


@app.route('/api/send-custom-email', methods=['POST'])
def send_custom_email():
    """
    Erweiterte E-Mail-Funktion mit benutzerdefinierten Optionen
    
    JSON Payload:
    {
        "employee_id": "uuid",
        "employee_name": "Name",
        "date_from": "dd.mm.yyyy",
        "date_to": "dd.mm.yyyy", 
        "email_to": "email1@example.com, email2@example.com",  # Mehrere mit Komma
        "email_cc": "cc1@example.com, cc2@example.com",        # Optional: CC Empfänger
        "email_subject": "Custom Subject",                      # Optional: benutzerdefinierter Betreff
        "attach_csv": true,                                     # Optional: CSV anhängen (default: true)
        "message": "Zusätzliche Nachricht",                    # Optional: persönliche Nachricht
        "employee_ids": ["uuid1", "uuid2"],                    # Optional: Multiple employee IDs
        "employee_names": ["Name1", "Name2"]                   # Optional: Multiple employee names
    }
    """
    try:
        if not middleware:
            return jsonify({
                'status': 'error',
                'message': 'Middleware nicht initialisiert'
            }), 500
        
        data = request.get_json()
        if not data:
            return jsonify({
                'status': 'error', 
                'message': 'JSON-Daten erforderlich'
            }), 400
        
        # Pflichtfelder
        date_from = data.get('date_from')  
        date_to = data.get('date_to')
        
        # Multi-Mitarbeiter Support
        employee_ids = data.get('employee_ids', [])
        employee_names = data.get('employee_names', [])
        
        # Fallback für einzelnen Mitarbeiter
        if not employee_ids:
            employee_id = data.get('employee_id')
            if employee_id:
                # Prüfe ob es komma-getrennte IDs sind
                if ',' in str(employee_id):
                    employee_ids = [e.strip() for e in employee_id.split(',')]
                else:
                    employee_ids = [employee_id]
        
        if not employee_names:
            employee_name = data.get('employee_name', 'Mitarbeiter')
            employee_names = [employee_name]
        
        if not employee_ids or not date_from or not date_to:
            return jsonify({
                'status': 'error',
                'message': 'Pflichtfelder: employee_id(s), date_from, date_to'
            }), 400
        
        # Optionale Felder
        custom_email = data.get('email_to')
        email_cc = data.get('email_cc', '')
        custom_subject = data.get('email_subject')
        attach_csv = data.get('attach_csv', True)
        custom_message = data.get('message', '')
        
        # Normalisiere Datums-Format
        date_from_normalized = middleware._normalize_date(date_from)
        date_to_normalized = middleware._normalize_date(date_to)
        
        print(f"=== MULTI-EMPLOYEE EMAIL REQUEST ===")
        print(f"Employees: {len(employee_ids)} selected")
        print(f"Names: {employee_names}")
        print(f"Period: {date_from_normalized} - {date_to_normalized}")
        print(f"Custom Email (To): {custom_email}")
        print(f"CC Recipients: {email_cc}")
        print(f"Attach CSV: {attach_csv}")
        
        # Sammle Daten für alle ausgewählten Mitarbeiter PARALLEL
        all_time_entries = []
        employee_data = []

        def fetch_email_entries(idx_emp):
            idx, emp_id = idx_emp
            emp_name = employee_names[idx] if idx < len(employee_names) else f"Mitarbeiter {idx+1}"
            try:
                entries = middleware.get_time_entries(
                    employee_id=emp_id,
                    date_from=date_from_normalized,
                    date_to=date_to_normalized
                )
                for entry in entries:
                    entry['name'] = emp_name
                    entry['employee_id'] = emp_id
                logger.info(f"  {emp_name}: {len(entries)} Eintraege")
                return (emp_name, emp_id, entries)
            except Exception as e:
                logger.warning(f"  Fehler bei {emp_name}: {e}")
                return (emp_name, emp_id, [])

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(fetch_email_entries, enumerate(employee_ids)))

        for emp_name, emp_id, entries in results:
            if entries:
                all_time_entries.extend(entries)
                employee_data.append({
                    'id': emp_id,
                    'name': emp_name,
                    'entry_count': len(entries)
                })
        
        if not all_time_entries:
            return jsonify({
                'status': 'error',
                'message': 'Keine Zeiteinträge für die ausgewählten Mitarbeiter gefunden'
            }), 400
        
        # Bestimme ob "Alle" ausgewählt sind
        all_employees = middleware.get_employees()
        is_all_selected = len(employee_ids) == len(all_employees)
        
        # Erstelle E-Mail mit Template
        result = middleware.send_multi_employee_report(
            employees=employee_data,
            time_entries=all_time_entries,
            date_from=date_from,
            date_to=date_to,
            custom_email=custom_email,
            email_cc=email_cc,
            custom_subject=custom_subject,
            attach_csv=attach_csv,
            custom_message=custom_message,
            is_all_employees=is_all_selected
        )
        
        return jsonify(result)
        
    except Exception as e:
        print(f"Error in send_custom_email: {e}")
        return jsonify({
            'status': 'error',
            'message': f'Server-Fehler: {str(e)}'
        }), 500


@app.route('/api/test-csv/<employee_id>')
def test_csv_structure(employee_id):
    """Test-Route für CSV-Struktur Validierung"""
    if not middleware:
        return jsonify({"error": "Middleware nicht initialisiert"}), 500
    
    # Test-Daten holen
    date_from = "01.10.2025"
    date_to = "31.10.2025"
    
    try:
        data = middleware.get_time_entries(
            employee_id=employee_id,
            date_from=middleware._normalize_date(date_from),
            date_to=middleware._normalize_date(date_to)
        )
        
        # CSV-Content erstellen
        csv_content = middleware.mail._create_csv_content(data, "Test Benutzer")
        
        return Response(
            csv_content,
            mimetype='text/csv',
            headers={"Content-disposition": "attachment; filename=test_struktur.csv"}
        )
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/download_csv')
def download_csv():
    """CSV-Download - Daten über Middleware (unterstützt mehrere Mitarbeiter)"""
    if not middleware:
        return jsonify({"error": "Middleware nicht initialisiert"}), 500
    
    # Filter aus Request-Parametern holen
    company = request.args.get('company')
    employee = request.args.get('employee')  # Kann komma-getrennt sein
    employee_ids = request.args.get('employee_ids')  # JSON array
    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to')
    
    if not date_from or not date_to:
        return jsonify({"error": "Fehlende Parameter: date_from, date_to"}), 400
    
    # Normalisiere Datum
    date_from_normalized = middleware._normalize_date(date_from)
    date_to_normalized = middleware._normalize_date(date_to)
    
    # Multi-Mitarbeiter Support
    employees_to_fetch = []
    
    if employee_ids:
        # JSON array von IDs
        import json
        try:
            employees_to_fetch = json.loads(employee_ids)
        except:
            employees_to_fetch = [employee_ids]
    elif employee:
        # Komma-getrennte IDs oder einzelne ID
        if ',' in str(employee):
            employees_to_fetch = [e.strip() for e in employee.split(',')]
        else:
            employees_to_fetch = [employee]
    
    if not employees_to_fetch:
        return jsonify({"error": "Fehlende Parameter: employee oder employee_ids"}), 400
    
    # Sammle alle Daten
    all_data = []
    for emp_id in employees_to_fetch:
        try:
            data = middleware.get_time_entries(
                employee_id=emp_id,
                date_from=date_from_normalized,
                date_to=date_to_normalized,
                organization_id=company
            )
            # Füge employee_id zu jedem Eintrag hinzu falls nicht vorhanden
            for entry in data:
                if 'employee_id' not in entry:
                    entry['employee_id'] = emp_id
            all_data.extend(data)
        except Exception as e:
            print(f"Fehler beim Laden für {emp_id}: {e}")
    
    if not all_data:
        return jsonify({"error": "Keine Daten gefunden"}), 404
    
    # CSV-Header erstellen - Original McTime Format
    headers = [
        'Personalnummer', 'Vorname', 'Nachname', 'Datum', 'Type',
        'Zeit Beginn', 'Zeit Ende', 'Pause', 'Summe mit Pause', 
        'Summe ohne Pause', 'Projektnummer', 'Auftragsnummer',
        'Projekt / Gruppenname', 'Kommentar'
    ]
    csv_data = [headers]
    
    # Daten hinzufügen im korrekten Format (verwende all_data statt data)
    for row in all_data:
        # Name aufteilen
        first_name = row.get('first_name', '')
        last_name = row.get('last_name', '')
        if not first_name and not last_name:
            name_parts = (row.get('name', '') or '').split(' ', 1)
            first_name = name_parts[0] if len(name_parts) > 0 else ''
            last_name = name_parts[1] if len(name_parts) > 1 else ''
        
        csv_data.append([
            _normalize_personnel_number(row.get('personnel_number', '')),  # Personalnummer nur numerisch
            first_name,                             # Vorname
            last_name,                              # Nachname
            row.get('date_formatted', ''),          # Datum (01.10.25)
            _format_time_type(row.get('type')),     # Type
            row.get('time_start', ''),              # Zeit Beginn (05:00)
            row.get('time_end', ''),                # Zeit Ende (18:00)
            f'"{row.get("breaks_formatted", "")}"' if row.get('breaks_formatted') else '', # Pause in Anführungszeichen
            row.get('total_hours_formatted', ''),   # Summe mit Pause (13:00)
            row.get('actual_hours_formatted', ''),  # Summe ohne Pause (12:00)
            '',                                     # Projektnummer
            '',                                     # Auftragsnummer
            row.get('project', ''),                 # Projekt / Gruppenname
            row.get('comment', '')                  # Kommentar
        ])
    
    # CSV in Memory erstellen mit Semicolon-Delimiter (wie im Original)
    output = io.StringIO()
    writer = csv.writer(output, delimiter=';')
    writer.writerows(csv_data)
    
    # Dateiname basierend auf Anzahl Mitarbeiter
    if len(employees_to_fetch) == 1:
        filename = "zeitdaten_export.csv"
    else:
        filename = f"zeitdaten_{len(employees_to_fetch)}_mitarbeiter_export.csv"
    
    # Response erstellen
    response = Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={"Content-disposition": f"attachment; filename={filename}"}
    )
    
    return response


@app.route('/api/test-chart-logic')
def test_chart_logic():
    """Nur zum Testen der internen Logik von get_chart_stats ohne echten API-Call."""
    from datetime import datetime
    
    try:
        # Simuliere einen einfachen Aufruf für den aktuellen Monat
        time_filter = 'month'
        month_param = datetime.now().strftime('%Y-%m')
        
        # Rufe die Logik auf (ohne den Request-Teil)
        # HINWEIS: Dies ist eine vereinfachte Version der Logik in get_chart_stats
        # um schnell Fehler zu finden.
        
        date_from = datetime.now().replace(day=1).strftime('%Y-%m-%d')
        date_to = datetime.now().strftime('%Y-%m-%d')

        if not backend_service:
            return jsonify({"error": "Backend Service nicht verfügbar"}), 500

        employees = backend_service.get_form_data().get('employees', [])
        if not employees:
            return jsonify({"error": "Keine Mitarbeiter gefunden"}), 500

        # Teste nur den ersten Mitarbeiter
        emp_id = employees[0].get('id')
        entries = backend_service.mctime_api.get_time_entries(
            employee_id=emp_id,
            date_from=date_from,
            date_to=date_to
        )

        return jsonify({
            "status": "success",
            "test_filter": time_filter,
            "test_month": month_param,
            "found_employees": len(employees),
            "first_employee_entries": len(entries),
            "first_employee_data": entries[:2] # zeige die ersten 2 Einträge
        })
    except Exception as e:
        import traceback
        return jsonify({
            "status": "error",
            "message": str(e),
            "trace": traceback.format_exc()
        }), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(debug=debug, host='0.0.0.0', port=port)
