import hmac
import json
import logging
import math
import os
import re
import secrets
from functools import wraps
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

import bcrypt
from dotenv import load_dotenv
from flask import Flask, abort, flash, redirect, render_template, request, session, url_for


load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_SECRET_KEY = os.getenv("SUPABASE_SECRET_KEY", "").strip()

MANAGED_TABLES = {
    "tours": {
        "title": "Аялал",
        "singular": "аялал",
        "table": "tours",
        "order": "name.asc",
        "search_fields": ("name", "destination", "description"),
        "fields": (
            {"name": "name", "label": "Аяллын нэр", "type": "text", "required": True},
            {"name": "destination", "label": "Чиглэл", "type": "text", "required": True},
            {"name": "description", "label": "Тайлбар", "type": "textarea", "required": True},
            {"name": "duration_days", "label": "Хоног", "type": "number", "required": True, "min": "1", "step": "1"},
            {"name": "price", "label": "Үнэ (₮)", "type": "number", "required": True, "min": "0", "step": "0.01"},
            {"name": "status", "label": "Төлөв", "type": "select", "required": True, "options": (("active", "Идэвхтэй"), ("inactive", "Идэвхгүй"))},
        ),
    },
    "staff": {
        "title": "Ажилтан",
        "singular": "ажилтан",
        "table": "staff",
        "order": "full_name.asc",
        "search_fields": ("full_name", "position", "email", "phone"),
        "fields": (
            {"name": "full_name", "label": "Овог, нэр", "type": "text", "required": True},
            {"name": "position", "label": "Албан тушаал", "type": "text", "required": True},
            {"name": "email", "label": "И-мэйл", "type": "email", "required": True},
            {"name": "phone", "label": "Утас", "type": "tel", "required": True},
            {"name": "status", "label": "Төлөв", "type": "select", "required": True, "options": (("active", "Ажиллаж байгаа"), ("inactive", "Идэвхгүй"))},
        ),
    },
}


class SupabaseError(Exception):
    pass


def create_app():
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.getenv("SESSION_SECRET") or secrets.token_hex(32),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "").lower() == "true",
    )

    def request_supabase(table, query, method="GET", payload=None):
        parsed_url = urlparse(SUPABASE_URL)
        if (
            parsed_url.scheme not in {"http", "https"}
            or not parsed_url.netloc
            or not SUPABASE_SECRET_KEY
            or SUPABASE_SECRET_KEY.startswith("YOUR_")
        ):
            raise SupabaseError("Supabase-ийн URL эсвэл түлхүүр тохируулаагүй байна.")

        url = f"{SUPABASE_URL}/rest/v1/{table}?{urlencode(query)}"
        api_request = Request(
            url,
            data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            method=method,
            headers={
                "apikey": SUPABASE_SECRET_KEY,
                "Authorization": f"Bearer {SUPABASE_SECRET_KEY}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            },
        )
        try:
            with urlopen(api_request, timeout=10) as response:
                response_body = response.read().decode("utf-8")
                data = json.loads(response_body) if response_body else []
        except HTTPError as error:
            logger.error("Supabase returned HTTP %s for table %s", error.code, table)
            raise SupabaseError(f"Supabase хүсэлт HTTP {error.code} алдаатай.") from error
        except URLError as error:
            logger.error("Could not connect to Supabase: %s", error.reason)
            raise SupabaseError("Supabase-т холбогдож чадсангүй.") from error
        except json.JSONDecodeError as error:
            logger.error("Supabase returned an invalid JSON response for table %s", table)
            raise SupabaseError("Supabase-аас буруу хариу ирлээ.") from error

        if not isinstance(data, list):
            raise SupabaseError("Supabase-аас хүлээгдээгүй хариу ирлээ.")
        return data

    def find_user_by_email(email):
        users = request_supabase(
            "users",
            {"select": "email,password_hash", "email": f"eq.{email}", "limit": "1"},
        )
        return users[0] if users else None

    def fetch_drafts():
        return request_supabase(
            "content_drafts",
            {
                "select": "id,title,body,status,created_at",
                "order": "created_at.desc",
            },
        )

    def update_draft_status(draft_id, status):
        updated_drafts = request_supabase(
            "content_drafts",
            {
                "id": f"eq.{draft_id}",
                "status": "in.(review,pending)",
                "select": "id,status",
            },
            method="PATCH",
            payload={"status": status},
        )
        if not updated_drafts:
            raise SupabaseError("Энэ нийтлэл шийдвэрлэгдсэн эсвэл олдсонгүй.")

    def fetch_bookings():
        return request_supabase(
            "bookings",
            {
                "select": "id,customer_name,contact_email,booking_date,party_size,status",
                "order": "booking_date.desc",
            },
        )

    def get_managed_table(entity):
        config = MANAGED_TABLES.get(entity)
        if config is None:
            abort(404)
        return config

    def managed_fields(config):
        return ",".join(["id", *(field["name"] for field in config["fields"])])

    def fetch_managed_records(config):
        return request_supabase(
            config["table"],
            {"select": managed_fields(config), "order": config["order"]},
        )

    def fetch_managed_record(config, record_id):
        records = request_supabase(
            config["table"],
            {"select": managed_fields(config), "id": f"eq.{record_id}", "limit": "1"},
        )
        return records[0] if records else None

    def save_managed_record(config, values, record_id=None):
        if record_id is None:
            saved_records = request_supabase(
                config["table"], {"select": "id"}, method="POST", payload=values
            )
        else:
            saved_records = request_supabase(
                config["table"],
                {"id": f"eq.{record_id}", "select": "id"},
                method="PATCH",
                payload=values,
            )
        if not saved_records:
            raise SupabaseError("Бүртгэл хадгалагдсангүй.")

    def delete_managed_record(config, record_id):
        deleted_records = request_supabase(
            config["table"],
            {"id": f"eq.{record_id}", "select": "id"},
            method="DELETE",
        )
        if not deleted_records:
            raise SupabaseError("Устгах бүртгэл олдсонгүй.")

    def read_managed_form(config):
        values = {}
        errors = []
        for field in config["fields"]:
            value = request.form.get(field["name"], "").strip()
            if field.get("required") and not value:
                errors.append(f"{field['label']} талбарыг бөглөнө үү.")
            if field["type"] == "number" and value:
                try:
                    number = float(value)
                    if not math.isfinite(number) or number < float(field.get("min", "-inf")):
                        errors.append(f"{field['label']} утга буруу байна.")
                    if field.get("step") == "1" and not number.is_integer():
                        errors.append(f"{field['label']} бүхэл тоо байх ёстой.")
                except ValueError:
                    errors.append(f"{field['label']} тоон утга байх ёстой.")
            if field["type"] == "select" and value not in {option[0] for option in field["options"]}:
                errors.append(f"{field['label']} сонголт буруу байна.")
            if field["type"] == "email" and value and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
                errors.append(f"{field['label']} хаяг буруу байна.")
            values[field["name"]] = value
        return values, errors

    def render_managed_records(entity, config, search_query="", record=None, form_values=None, form_errors=None, status=200):
        try:
            records = fetch_managed_records(config)
        except SupabaseError as error:
            logger.warning("Could not load %s: %s", config["table"], error)
            records = None
            flash(f"{config['title']} жагсаалтыг татаж чадсангүй. Supabase-ийн тохиргоо болон RLS эрхийг шалгана уу.", "error")

        if records is not None and search_query:
            normalized_query = search_query.casefold()
            records = [
                item for item in records
                if any(normalized_query in str(item.get(key, "")).casefold() for key in config["search_fields"])
            ]

        return render_template(
            "manage.html",
            entity=entity,
            config=config,
            records=records,
            search_query=search_query,
            record=record,
            form_values=form_values or (record or {}),
            form_errors=form_errors or [],
            csrf_token=csrf_token(),
        ), status

    def csrf_token():
        return session.setdefault("csrf_token", secrets.token_urlsafe(32))

    def render_dashboard(page, drafts=None, bookings=None):
        return render_template(
            "dashboard.html",
            active_page=page,
            drafts=drafts,
            bookings=bookings,
            user_email=session["user_email"],
            csrf_token=csrf_token(),
        )

    def validate_csrf():
        submitted = request.form.get("csrf_token", "")
        expected = session.get("csrf_token", "")
        if not submitted or not expected or not hmac.compare_digest(submitted, expected):
            abort(400)

    def login_required(view):
        @wraps(view)
        def wrapped_view(*args, **kwargs):
            if "user_email" not in session:
                return redirect(url_for("login"))
            return view(*args, **kwargs)

        return wrapped_view

    @app.get("/")
    def index():
        if "user_email" in session:
            return redirect(url_for("dashboard"))
        return redirect(url_for("login"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        token = csrf_token()
        if request.method == "POST":
            validate_csrf()
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")

            if not email or len(email) > 254 or not password or len(password.encode("utf-8")) > 72:
                flash("И-мэйл эсвэл нууц үг буруу байна.", "error")
                return render_template("login.html", csrf_token=token), 401

            try:
                user = find_user_by_email(email)
            except SupabaseError as error:
                logger.warning("Login could not query Supabase: %s", error)
                flash("Өгөгдлийн сантай холбогдсонгүй. .env тохиргоо болон хүснэгтийн эрхийг шалгана уу.", "error")
                return render_template("login.html", csrf_token=token), 503

            valid_password = False
            if user:
                try:
                    valid_password = bcrypt.checkpw(
                        password.encode("utf-8"),
                        user["password_hash"].encode("ascii"),
                    )
                except (ValueError, UnicodeEncodeError, KeyError, AttributeError):
                    logger.exception("User record has an invalid bcrypt password hash")
                    flash("Хэрэглэгчийн нууц үгийн мэдээлэл буруу байна.", "error")
                    return render_template("login.html", csrf_token=token), 500

            if not user or not valid_password:
                flash("И-мэйл эсвэл нууц үг буруу байна.", "error")
                return render_template("login.html", csrf_token=token), 401

            session.clear()
            session["user_email"] = user["email"]
            csrf_token()
            return redirect(url_for("dashboard"))

        return render_template("login.html", csrf_token=token)

    @app.get("/dashboard")
    @login_required
    def dashboard():
        try:
            drafts = fetch_drafts()
        except SupabaseError as error:
            logger.warning("Could not load content drafts: %s", error)
            drafts = None
            flash("Ноорог пост татаж чадсангүй. Supabase-ийн тохиргоо болон RLS эрхийг шалгана уу.", "error")
        return render_dashboard("content", drafts=drafts)

    @app.post("/drafts/<int:draft_id>/<decision>")
    @login_required
    def decide_draft(draft_id, decision):
        validate_csrf()
        decisions = {
            "approve": ("approved", "Нийтлэлийг баталлаа."),
            "reject": ("rejected", "Нийтлэлээс татгалзлаа."),
        }
        if decision not in decisions:
            abort(404)

        status, success_message = decisions[decision]
        try:
            update_draft_status(draft_id, status)
        except SupabaseError as error:
            logger.warning("Could not update draft %s: %s", draft_id, error)
            flash("Нийтлэлийн төлөвийг өөрчилж чадсангүй. Жагсаалтыг шинэчлээд дахин оролдоно уу.", "error")
        else:
            flash(success_message, "success")
        return redirect(url_for("dashboard"))

    @app.get("/bookings")
    @login_required
    def bookings():
        try:
            booking_list = fetch_bookings()
        except SupabaseError as error:
            logger.warning("Could not load bookings: %s", error)
            booking_list = None
            flash("Захиалга татаж чадсангүй. Supabase-ийн тохиргоо болон RLS эрхийг шалгана уу.", "error")
        return render_dashboard("bookings", bookings=booking_list)

    @app.get("/facebook")
    @login_required
    def facebook():
        return render_dashboard("facebook")

    @app.get("/chat")
    @login_required
    def chat():
        return render_dashboard("chat")

    @app.route("/<entity>", methods=["GET", "POST"])
    @login_required
    def manage_records(entity):
        config = get_managed_table(entity)
        search_query = request.args.get("q", "").strip()
        if request.method == "POST":
            validate_csrf()
            values, errors = read_managed_form(config)
            if errors:
                return render_managed_records(
                    entity, config, search_query, form_values=values, form_errors=errors, status=400
                )
            try:
                save_managed_record(config, values)
            except SupabaseError as error:
                logger.warning("Could not add a record to %s: %s", config["table"], error)
                flash("Шинэ бүртгэлийг хадгалж чадсангүй.", "error")
            else:
                flash(f"Шинэ {config['singular']} нэмлээ.", "success")
            return redirect(url_for("manage_records", entity=entity, q=search_query))
        return render_managed_records(entity, config, search_query)

    @app.route("/<entity>/<int:record_id>/edit", methods=["GET", "POST"])
    @login_required
    def edit_record(entity, record_id):
        config = get_managed_table(entity)
        search_query = request.args.get("q", "").strip()
        try:
            record = fetch_managed_record(config, record_id)
        except SupabaseError as error:
            logger.warning("Could not load %s record %s: %s", config["table"], record_id, error)
            flash("Бүртгэлийг уншиж чадсангүй.", "error")
            return redirect(url_for("manage_records", entity=entity, q=search_query))
        if record is None:
            abort(404)

        if request.method == "POST":
            validate_csrf()
            values, errors = read_managed_form(config)
            if errors:
                return render_managed_records(
                    entity,
                    config,
                    search_query,
                    record=record,
                    form_values=values,
                    form_errors=errors,
                    status=400,
                )
            try:
                save_managed_record(config, values, record_id)
            except SupabaseError as error:
                logger.warning("Could not update %s record %s: %s", config["table"], record_id, error)
                flash("Өөрчлөлтийг хадгалж чадсангүй.", "error")
            else:
                flash(f"{config['singular'].capitalize()}-ийн мэдээллийг шинэчиллээ.", "success")
            return redirect(url_for("manage_records", entity=entity, q=search_query))

        return render_managed_records(entity, config, search_query, record=record)

    @app.get("/<entity>/<int:record_id>/delete")
    @login_required
    def confirm_delete_record(entity, record_id):
        config = get_managed_table(entity)
        search_query = request.args.get("q", "").strip()
        try:
            record = fetch_managed_record(config, record_id)
        except SupabaseError as error:
            logger.warning("Could not load %s record %s for deletion: %s", config["table"], record_id, error)
            flash("Устгах бүртгэлийг уншиж чадсангүй.", "error")
            return redirect(url_for("manage_records", entity=entity))
        if record is None:
            abort(404)
        return render_template(
            "confirm_delete.html",
            entity=entity,
            config=config,
            record=record,
            search_query=search_query,
            csrf_token=csrf_token(),
        )

    @app.post("/<entity>/<int:record_id>/delete")
    @login_required
    def delete_record(entity, record_id):
        validate_csrf()
        if request.form.get("confirm_delete") != "yes":
            abort(400)
        config = get_managed_table(entity)
        search_query = request.form.get("return_q", "").strip()
        try:
            delete_managed_record(config, record_id)
        except SupabaseError as error:
            logger.warning("Could not delete %s record %s: %s", config["table"], record_id, error)
            flash("Бүртгэлийг устгаж чадсангүй.", "error")
        else:
            flash(f"{config['singular'].capitalize()}-ийг устгалаа.", "success")
        return redirect(url_for("manage_records", entity=entity, q=search_query))

    @app.post("/logout")
    @login_required
    def logout():
        validate_csrf()
        session.clear()
        return redirect(url_for("login"))

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")))
