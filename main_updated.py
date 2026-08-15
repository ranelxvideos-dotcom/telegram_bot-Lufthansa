"""Lufthansa Airlines Telegram bot.

The application intentionally lives in one file so it can be copied directly
to Replit and started with:

    python -m telegram_bot.main

Secrets are read from environment variables. The bot stores its local users,
administrators, and managed flights in SQLite.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
import secrets
import sqlite3
from datetime import date, datetime, timezone
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from .i18n import (
    DEFAULT_LANGUAGE,
    SUPPORTED_LANGUAGES,
    language_menu,
    translate_markup,
    translate_text,
)
from .timezones import (
    AIRPORTS_BY_CODE,
    WEATHER_LOCATIONS,
    format_airport_clocks,
    format_flight_local_times,
    normalize_fictional_airport,
)


LOGGER = logging.getLogger("lufthansa_bot")
TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
LUFTHANSA_ID_ENV = "LUFTHANSA_CLIENT_ID"
LUFTHANSA_SECRET_ENV = "LUFTHANSA_CLIENT_SECRET"
ADMIN_CODE_ENV = "ADMIN_SETUP_CODE"
OWNER_ID_ENV = "OWNER_TELEGRAM_ID"
DATABASE_PATH = Path(
    os.getenv("BOT_DATABASE_PATH", str(Path(__file__).with_name("bot.db")))
)

FLIGHT_NUMBER_RE = re.compile(r"^[A-Z]{2}\d{1,4}[A-Z]?$")
AIRPORT_RE = re.compile(r"^[A-Z]{3}$")
LOCAL_FLIGHT_FORMAT = (
    "Формат: `LH123 | GRC | IZO | 2026-08-20 14:00 | "
    "2026-08-20 11:00 | Планируется`"
)


def now_iso() -> str:
    """Return a sortable UTC timestamp."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_secret(name: str) -> str:
    """Read a required secret without ever logging its value."""
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Не задана обязательная переменная окружения: {name}")
    return value


def normalized_flight_status(status: str) -> str:
    return " ".join(str(status).lower().replace("ё", "е").split())


def flight_status_phase(status: str) -> str:
    """Map a free-form admin status to the flight's current lifecycle phase."""
    normalized = normalized_flight_status(status)
    if not normalized:
        return "unknown"
    if any(marker in normalized for marker in ("отмен", "удал", "cancel")):
        return "cancelled"
    if any(
        marker in normalized
        for marker in ("прилет", "прибыл", "сел", "призем", "landed")
    ):
        return "arrived"
    if any(
        marker in normalized
        for marker in ("на посад", "посад", "прибыва", "landing", "arriving")
    ):
        return "landing"
    if any(
        marker in normalized
        for marker in (
            "в полет",
            "в пути",
            "полет",
            "летит",
            "вылет",
            "вылетел",
            "departed",
            "in flight",
            "en route",
        )
    ):
        return "in_flight"
    if any(marker in normalized for marker in ("задерж", "delay")):
        return "delayed"
    if any(
        marker in normalized
        for marker in ("план", "заплан", "ожида", "регистрац", "scheduled")
    ):
        return "scheduled"
    return "unknown"


def is_visible_on_flight_board(status: str, direction: str) -> bool:
    phase = flight_status_phase(status)
    if direction == "departures":
        return phase in {"in_flight", "landing"}
    return phase in {"in_flight", "landing", "arrived"}


class Database:
    """Small SQLite repository kept in this file for easy Replit setup."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;

                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    language TEXT
                );

                CREATE TABLE IF NOT EXISTS admins (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    is_owner INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS managed_flights (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    flight_number TEXT NOT NULL,
                    departure_airport TEXT NOT NULL,
                    arrival_airport TEXT NOT NULL,
                    scheduled_at TEXT NOT NULL,
                    arrival_at TEXT,
                    status TEXT NOT NULL,
                    terminal TEXT,
                    gate TEXT,
                    created_by INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS flight_bookings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    flight_id INTEGER NOT NULL,
                    ticket_code TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    UNIQUE(user_id, flight_id)
                );
                """
            )
            user_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(users)").fetchall()
            }
            if "language" not in user_columns:
                connection.execute("ALTER TABLE users ADD COLUMN language TEXT")
            flight_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(managed_flights)"
                ).fetchall()
            }
            if "arrival_at" not in flight_columns:
                connection.execute(
                    "ALTER TABLE managed_flights ADD COLUMN arrival_at TEXT"
                )

    def upsert_user(self, user_id: int, username: str | None, first_name: str) -> None:
        timestamp = now_iso()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO users (user_id, username, first_name, created_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    active = 1,
                    last_seen_at = excluded.last_seen_at
                """,
                (user_id, username, first_name, timestamp, timestamp),
            )

    def user_ids(self) -> list[int]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT user_id FROM users WHERE active = 1"
            ).fetchall()
        return [int(row["user_id"]) for row in rows]

    def user_language(self, user_id: int) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT language FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
        if not row:
            return None
        language = str(row["language"] or "").strip().lower()
        return language if language in SUPPORTED_LANGUAGES else None

    def set_user_language(self, user_id: int, language: str) -> None:
        if language not in SUPPORTED_LANGUAGES:
            return
        with self.connect() as connection:
            connection.execute(
                "UPDATE users SET language = ? WHERE user_id = ?",
                (language, user_id),
            )

    def add_admin(
        self,
        user_id: int,
        username: str | None,
        is_owner: bool = False,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO admins (user_id, username, is_owner, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET username = excluded.username
                """,
                (user_id, username, int(is_owner), now_iso()),
            )

    def ensure_owner(self, user_id: int) -> None:
        """Make the configured Telegram account the only owner."""
        with self.connect() as connection:
            connection.execute("UPDATE admins SET is_owner = 0")
            connection.execute(
                """
                INSERT INTO admins (user_id, username, is_owner, created_at)
                VALUES (?, NULL, 1, ?)
                ON CONFLICT(user_id) DO UPDATE SET is_owner = 1
                """,
                (user_id, now_iso()),
            )

    def admin(self, user_id: int) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM admins WHERE user_id = ?", (user_id,)
            ).fetchone()

    def admins(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM admins ORDER BY is_owner DESC, created_at"
            ).fetchall()

    def remove_admin(self, user_id: int) -> bool:
        with self.connect() as connection:
            result = connection.execute(
                "DELETE FROM admins WHERE user_id = ? AND is_owner = 0",
                (user_id,),
            )
        return result.rowcount > 0

    def create_flight(
        self,
        flight_number: str,
        departure: str,
        arrival: str,
        scheduled_at: str,
        arrival_at: str | None,
        status: str,
        terminal: str,
        gate: str,
        created_by: int,
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO managed_flights (
                    flight_number, departure_airport, arrival_airport,
                    scheduled_at, arrival_at, status, terminal, gate,
                    created_by, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    flight_number,
                    departure,
                    arrival,
                    scheduled_at,
                    arrival_at,
                    status,
                    terminal,
                    gate,
                    created_by,
                    now_iso(),
                ),
            )
        return int(cursor.lastrowid)

    def flights(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM managed_flights ORDER BY scheduled_at, id"
            ).fetchall()

    def available_flights(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT * FROM managed_flights
                WHERE LOWER(status) NOT LIKE '%отмен%'
                  AND LOWER(status) NOT LIKE '%удал%'
                ORDER BY scheduled_at, id
                """
            ).fetchall()

    def active_flights_by_airport(
        self, airport: str, direction: str
    ) -> list[sqlite3.Row]:
        airport_column = (
            "departure_airport" if direction == "departures" else "arrival_airport"
        )
        with self.connect() as connection:
            flights = connection.execute(
                f"""
                SELECT * FROM managed_flights
                WHERE {airport_column} = ?
                ORDER BY scheduled_at, id
                """,
                (airport,),
            ).fetchall()
        return [
            flight
            for flight in flights
            if is_visible_on_flight_board(str(flight["status"]), direction)
        ]

    def flight(self, flight_id: int) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM managed_flights WHERE id = ?", (flight_id,)
            ).fetchone()

    def update_flight(self, flight_id: int, values: dict[str, str]) -> bool:
        allowed = {
            "flight_number",
            "departure_airport",
            "arrival_airport",
            "scheduled_at",
            "arrival_at",
            "status",
            "terminal",
            "gate",
        }
        safe_values = {key: value for key, value in values.items() if key in allowed}
        if not safe_values:
            return False
        with self.connect() as connection:
            current = connection.execute(
                """
                SELECT departure_airport, arrival_airport, status
                FROM managed_flights
                WHERE id = ?
                """,
                (flight_id,),
            ).fetchone()
            if not current:
                return False

            previous_phase = flight_status_phase(str(current["status"]))
            next_phase = flight_status_phase(
                str(safe_values.get("status", current["status"]))
            )
            route_is_being_edited = any(
                key in safe_values for key in ("departure_airport", "arrival_airport")
            )
            if (
                not route_is_being_edited
                and previous_phase == "arrived"
                and next_phase == "in_flight"
            ):
                # A new departure after landing starts the return leg. Swapping
                # the active route makes the same managed flight cycle between
                # both airports without requiring a second flight record.
                safe_values["departure_airport"] = str(current["arrival_airport"])
                safe_values["arrival_airport"] = str(current["departure_airport"])

        assignments = ", ".join(f"{key} = ?" for key in safe_values)
        parameters = [safe_values[key] for key in safe_values]
        parameters.extend([now_iso(), flight_id])
        with self.connect() as connection:
            result = connection.execute(
                f"""
                UPDATE managed_flights
                SET {assignments}, updated_at = ?
                WHERE id = ?
                """,
                parameters,
            )
        return result.rowcount > 0

    def delete_flight(self, flight_id: int) -> bool:
        with self.connect() as connection:
            result = connection.execute(
                "DELETE FROM managed_flights WHERE id = ?", (flight_id,)
            )
        return result.rowcount > 0

    def admin_ids(self) -> list[int]:
        with self.connect() as connection:
            rows = connection.execute("SELECT user_id FROM admins").fetchall()
        return [int(row["user_id"]) for row in rows]

    def booking_user_ids(self, flight_id: int) -> list[int]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT user_id FROM flight_bookings WHERE flight_id = ?",
                (flight_id,),
            ).fetchall()
        return [int(row["user_id"]) for row in rows]

    def create_or_get_booking(
        self, user_id: int, flight_id: int
    ) -> tuple[sqlite3.Row | None, bool]:
        with self.connect() as connection:
            existing = connection.execute(
                """
                SELECT * FROM flight_bookings
                WHERE user_id = ? AND flight_id = ?
                """,
                (user_id, flight_id),
            ).fetchone()
            if existing:
                return existing, False

            ticket_code = f"LH-{secrets.token_hex(4).upper()}"
            try:
                connection.execute(
                    """
                    INSERT INTO flight_bookings (
                        user_id, flight_id, ticket_code, created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (user_id, flight_id, ticket_code, now_iso()),
                )
            except sqlite3.IntegrityError:
                existing = connection.execute(
                    """
                    SELECT * FROM flight_bookings
                    WHERE user_id = ? AND flight_id = ?
                    """,
                    (user_id, flight_id),
                ).fetchone()
                return existing, False
            booking = connection.execute(
                "SELECT * FROM flight_bookings WHERE ticket_code = ?",
                (ticket_code,),
            ).fetchone()
        return booking, True


class LufthansaClient:
    """Minimal asynchronous client for Lufthansa Open API v1."""

    token_url = "https://api.lufthansa.com/v1/oauth/token"
    api_url = "https://api.lufthansa.com/v1"

    def __init__(self, client_id: str, client_secret: str) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self._access_token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()

    async def access_token(self) -> str:
        current_time = datetime.now(timezone.utc).timestamp()
        if self._access_token and current_time < self._token_expires_at:
            return self._access_token

        async with self._token_lock:
            current_time = datetime.now(timezone.utc).timestamp()
            if self._access_token and current_time < self._token_expires_at:
                return self._access_token
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(
                    self.token_url,
                    data={"grant_type": "client_credentials"},
                    auth=(self.client_id, self.client_secret),
                )
                response.raise_for_status()
                payload = response.json()
            token = payload.get("access_token")
            if not token:
                raise RuntimeError("Lufthansa API не вернул access_token")
            expires_in = int(payload.get("expires_in", 1800))
            self._access_token = str(token)
            self._token_expires_at = (
                datetime.now(timezone.utc).timestamp() + max(expires_in - 60, 60)
            )
            return self._access_token

    async def get_json(self, path: str) -> dict[str, Any]:
        token = await self.access_token()
        async with httpx.AsyncClient(timeout=25) as client:
            response = await client.get(
                f"{self.api_url}/{path.lstrip('/')}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
            )
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Lufthansa API вернул неожиданный формат данных")
        return payload

    async def flight_status(self, flight_number: str) -> dict[str, Any]:
        today = date.today().isoformat()
        encoded_number = quote(flight_number, safe="")
        return await self.get_json(f"flightstatus/{encoded_number}/{today}")

    async def airport_flights(
        self, airport: str, direction: str
    ) -> dict[str, Any]:
        today = date.today().isoformat()
        encoded_airport = quote(airport, safe="")
        return await self.get_json(
            f"flightstatus/{encoded_airport}/{direction}/{today}/24"
        )


class WeatherClient:
    """Small resilient client for the keyless Open-Meteo weather API."""

    api_url = "https://api.open-meteo.com/v1/forecast"

    async def current_weather(self, airport: str) -> dict[str, Any]:
        location = WEATHER_LOCATIONS.get(airport)
        if not location:
            raise ValueError(f"Нет координат погоды для аэропорта {airport}")

        params = {
            **location,
            "current": (
                "temperature_2m,relative_humidity_2m,apparent_temperature,"
                "weather_code,wind_speed_10m"
            ),
            "timezone": "auto",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(self.api_url, params=params)
            response.raise_for_status()
            payload = response.json()

        if not isinstance(payload, dict):
            raise RuntimeError("Погодный API вернул неожиданный формат данных")
        current = payload.get("current") or payload.get("current_weather")
        if not isinstance(current, dict):
            raise RuntimeError("Погодный API не вернул текущую погоду")
        return current


def weather_description(code: Any) -> str:
    """Translate WMO weather codes while tolerating API response changes."""
    try:
        weather_code = int(code)
    except (TypeError, ValueError):
        return "условия не определены"
    if weather_code == 0:
        return "ясно"
    if weather_code in {1, 2, 3}:
        return "переменная облачность"
    if weather_code in {45, 48}:
        return "туман"
    if weather_code in {51, 53, 55, 56, 57}:
        return "морось"
    if weather_code in {61, 63, 65, 66, 67}:
        return "дождь"
    if weather_code in {71, 73, 75, 77, 85, 86}:
        return "снег"
    if weather_code in {80, 81, 82}:
        return "ливень"
    if weather_code in {95, 96, 99}:
        return "гроза"
    return "условия не определены"


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✈️ Найти рейс", callback_data="find_flight"),
                InlineKeyboardButton("🔎 Информация о рейсе", callback_data="flight_info"),
            ],
            [
                InlineKeyboardButton("🛫 Вылеты", callback_data="departures"),
                InlineKeyboardButton("🛬 Прилёты", callback_data="arrivals"),
            ],
            [
                InlineKeyboardButton("🔐 Панель администратора", callback_data="admin"),
                InlineKeyboardButton("ℹ️ Помощь", callback_data="help"),
            ],
            [
                InlineKeyboardButton(
                    "🌐 Язык / Language", callback_data="language"
                )
            ],
            [
                InlineKeyboardButton(
                    "🕒 Время аэропортов", callback_data="airport_times"
                )
            ],
            [
                InlineKeyboardButton(
                    "🌦 Погода в аэропортах", callback_data="weather"
                )
            ],
        ]
    )


def airport_times_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔄 Обновить", callback_data="airport_times_refresh"
                )
            ],
            [InlineKeyboardButton("◀️ Главное меню", callback_data="back")],
        ]
    )


def weather_menu(selected_airport: str | None = None) -> InlineKeyboardMarkup:
    rows = []
    airport_buttons = [
        InlineKeyboardButton(
            f"🌦 {code} — {airport['name']}",
            callback_data=f"weather:{code}",
        )
        for code, airport in AIRPORTS_BY_CODE.items()
    ]
    for index in range(0, len(airport_buttons), 2):
        rows.append(airport_buttons[index : index + 2])
    if selected_airport:
        rows.append(
            [
                InlineKeyboardButton(
                    "🔄 Обновить",
                    callback_data=f"weather_refresh:{selected_airport}",
                )
            ]
        )
    rows.append([InlineKeyboardButton("◀️ Главное меню", callback_data="back")])
    return InlineKeyboardMarkup(rows)


def back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("◀️ Назад", callback_data="back")]]
    )


def available_flights_menu(flights: list[sqlite3.Row]) -> InlineKeyboardMarkup:
    rows = []
    for flight in flights:
        label = (
            f"✈️ {flight['flight_number']} • "
            f"{flight['departure_airport']} → {flight['arrival_airport']}"
        )
        rows.append(
            [InlineKeyboardButton(label[:60], callback_data=f"user_flight:{flight['id']}")]
        )
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="back")])
    return InlineKeyboardMarkup(rows)


def flight_board_menu(flights: list[sqlite3.Row]) -> InlineKeyboardMarkup:
    rows = []
    for flight in flights:
        label = (
            f"✈️ {flight['flight_number']} • "
            f"{flight['departure_airport']} → {flight['arrival_airport']} • "
            f"{flight['status']}"
        )
        rows.append(
            [
                InlineKeyboardButton(
                    label[:60],
                    callback_data=f"info_local_flight:{flight['id']}",
                )
            ]
        )
    rows.append([InlineKeyboardButton("◀️ Главное меню", callback_data="back")])
    return InlineKeyboardMarkup(rows)


def flight_information_menu(flights: list[sqlite3.Row]) -> InlineKeyboardMarkup:
    rows = []
    for flight in flights:
        label = (
            f"✈️ {flight['flight_number']} • "
            f"{flight['departure_airport']} → {flight['arrival_airport']} • "
            f"{flight['status']}"
        )
        rows.append(
            [
                InlineKeyboardButton(
                    label[:60],
                    callback_data=f"info_local_flight:{flight['id']}",
                )
            ]
        )
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    "🔎 Найти рейс Lufthansa по номеру",
                    callback_data="flight_info_search",
                )
            ],
            [InlineKeyboardButton("◀️ Главное меню", callback_data="back")],
        ]
    )
    return InlineKeyboardMarkup(rows)


def ticket_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✈️ Другие рейсы", callback_data="available_flights")],
            [InlineKeyboardButton("◀️ Главное меню", callback_data="back")],
        ]
    )


def admin_broadcast_menu(flights: list[sqlite3.Row]) -> InlineKeyboardMarkup:
    rows = []
    for flight in flights:
        label = (
            f"📣 {flight['flight_number']} • "
            f"{flight['departure_airport']} → {flight['arrival_airport']}"
        )
        rows.append(
            [
                InlineKeyboardButton(
                    label[:60],
                    callback_data=f"admin_broadcast_flight:{flight['id']}",
                )
            ]
        )
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="admin")])
    return InlineKeyboardMarkup(rows)


def admin_menu(is_owner: bool) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("➕ Добавить рейс", callback_data="admin_add_flight"),
            InlineKeyboardButton("✏️ Изменить рейс", callback_data="admin_edit_flight"),
        ],
        [
            InlineKeyboardButton("❌ Отменить рейс", callback_data="admin_cancel_flight"),
            InlineKeyboardButton("🗑 Удалить рейс", callback_data="admin_delete_flight"),
        ],
        [InlineKeyboardButton("📋 Список рейсов", callback_data="admin_list_flights")],
        [
            InlineKeyboardButton(
                "📣 Рассылка пассажирам",
                callback_data="admin_broadcast",
            )
        ],
    ]
    if is_owner:
        rows.append(
            [
                InlineKeyboardButton(
                    "👥 Управление администраторами",
                    callback_data="admin_manage",
                )
            ]
        )
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="back")])
    return InlineKeyboardMarkup(rows)


def format_value(value: Any, fallback: str = "не указано") -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    return text or fallback


def nested(data: dict[str, Any], *keys: str) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def extract_api_flights(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Support the usual Lufthansa response shape and safe empty responses."""
    candidates = [
        nested(payload, "FlightStatusResource", "Flights", "FlightStatus"),
        nested(payload, "Flights", "FlightStatus"),
        payload.get("FlightStatus"),
    ]
    for candidate in candidates:
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
        if isinstance(candidate, dict):
            return [candidate]
    return []


def api_flight_text(flight: dict[str, Any]) -> str:
    marketing = nested(flight, "MarketingCarrier") or {}
    operating = nested(flight, "OperatingCarrier") or {}
    departure = flight.get("Departure") or {}
    arrival = flight.get("Arrival") or {}

    number = (
        f"{format_value(marketing.get('AirlineID'), '')}"
        f"{format_value(marketing.get('FlightNumber'), '')}"
    ).strip()
    if not number:
        number = format_value(flight.get("FlightNumber"))

    def airport_text(section: dict[str, Any]) -> str:
        airport = format_value(section.get("AirportCode"))
        terminal = nested(section, "Terminal", "Name") or section.get("Terminal")
        gate = section.get("Gate")
        extras = []
        if terminal:
            extras.append(f"терминал {terminal}")
        if gate:
            extras.append(f"гейт {gate}")
        return f"{airport}" + (f" ({', '.join(map(str, extras))})" if extras else "")

    departure_time = nested(departure, "ScheduledTimeLocal", "DateTime")
    departure_time = departure_time or departure.get("ScheduledTime")
    arrival_time = nested(arrival, "ScheduledTimeLocal", "DateTime")
    arrival_time = arrival_time or arrival.get("ScheduledTime")
    status = (
        nested(flight, "PublicStatus", "Code")
        or nested(flight, "PublicStatus", "Description")
        or flight.get("Status")
    )
    carrier = (
        marketing.get("AirlineID")
        or operating.get("AirlineID")
        or flight.get("Airline")
    )

    return (
        f"✈️ Рейс: <b>{number}</b>\n"
        f"🏢 Авиакомпания: {format_value(carrier, 'Lufthansa')}\n"
        f"🛫 Отправление: {airport_text(departure)}\n"
        f"🛬 Прибытие: {airport_text(arrival)}\n"
        f"🕒 Вылет: {format_value(departure_time)}\n"
        f"🕒 Прибытие: {format_value(arrival_time)}\n"
        f"📌 Статус: {format_value(status)}"
    )


def format_local_flight(flight: sqlite3.Row) -> str:
    details = [
        f"Терминал {flight['terminal']}" if flight["terminal"] else "",
        f"гейт {flight['gate']}" if flight["gate"] else "",
    ]
    suffix = f" ({', '.join(item for item in details if item)})" if any(details) else ""
    return (
        f"🆔 ID: <b>{flight['id']}</b>\n"
        f"✈️ Рейс: <b>{flight['flight_number']}</b>\n"
        f"🛫 {flight['departure_airport']} → 🛬 {flight['arrival_airport']}\n"
        f"{format_flight_local_times(str(flight['departure_airport']), str(flight['scheduled_at']), str(flight['arrival_airport']), str(flight['arrival_at']) if flight['arrival_at'] else None)}\n"
        f"📌 Статус: {flight['status']}{suffix}"
    )


def format_local_flight_information(flight: sqlite3.Row) -> str:
    departure = str(flight["departure_airport"])
    arrival = str(flight["arrival_airport"])
    phase = flight_status_phase(str(flight["status"]))
    if phase == "scheduled":
        location = f"ожидает вылета в {departure}"
    elif phase == "delayed":
        location = f"задержан в {departure}"
    elif phase == "in_flight":
        location = f"вылетел из {departure} — в полёте в {arrival}"
    elif phase == "landing":
        location = f"на посадке в {arrival}"
    elif phase == "arrived":
        location = f"находится в {arrival}"
    elif phase == "cancelled":
        location = f"рейс отменён в {departure}"
    else:
        location = f"маршрут {departure} → {arrival}"
    return format_local_flight(flight) + f"\n📍 Где находится: <b>{location}</b>"


def format_ticket(flight: sqlite3.Row, booking: sqlite3.Row) -> str:
    """Render a compact boarding-pass style ticket for Telegram."""
    terminal = str(flight["terminal"] or "—")
    gate = str(flight["gate"] or "—")
    return (
        "╭────────────────────────╮\n"
        "│  🎫 <b>LUFTHANSA AIRLINES</b>  │\n"
        "│     <b>BOARDING PASS</b>     │\n"
        "╰────────────────────────╯\n\n"
        f"✈️ <b>{flight['flight_number']}</b>\n"
        f"🛫 <b>{flight['departure_airport']}</b>  →  "
        f"🛬 <b>{flight['arrival_airport']}</b>\n\n"
        f"{format_flight_local_times(str(flight['departure_airport']), str(flight['scheduled_at']), str(flight['arrival_airport']), str(flight['arrival_at']) if flight['arrival_at'] else None)}\n"
        f"🚪 <b>Терминал:</b> {terminal}\n"
        f"🎟 <b>Гейт:</b> {gate}\n"
        f"📌 <b>Статус:</b> {flight['status']}\n\n"
        "────────────────────────\n"
        f"🔐 <b>Номер билета:</b> <code>{booking['ticket_code']}</code>\n"
        "Покажите этот билет при регистрации на рейс."
    )


def normalize_flight_number(value: str) -> str | None:
    normalized = re.sub(r"\s+", "", value.upper())
    return normalized if FLIGHT_NUMBER_RE.fullmatch(normalized) else None


def normalize_airport(value: str) -> str | None:
    normalized = value.strip().upper()
    fictional_code = normalize_fictional_airport(value)
    if fictional_code:
        return fictional_code
    return normalized if AIRPORT_RE.fullmatch(normalized) else None


def parse_local_flight(
    value: str,
) -> tuple[str, str, str, str, str | None, str, str, str] | None:
    parts = [part.strip() for part in value.split("|")]
    if len(parts) < 5:
        return None
    flight_number = normalize_flight_number(parts[0])
    departure = normalize_airport(parts[1])
    arrival = normalize_airport(parts[2])
    if (
        not flight_number
        or not departure
        or not arrival
        or departure == arrival
        or not parts[3]
        or not parts[4]
    ):
        return None
    if len(parts) >= 6:
        arrival_at = parts[4]
        status = parts[5]
        terminal = parts[6] if len(parts) > 6 else ""
        gate = parts[7] if len(parts) > 7 else ""
    else:
        arrival_at = None
        status = parts[4]
        terminal = parts[5] if len(parts) > 5 else ""
        gate = parts[6] if len(parts) > 6 else ""
    return (
        flight_number,
        departure,
        arrival,
        parts[3],
        arrival_at,
        status,
        terminal,
        gate,
    )


def parse_edit_values(value: str) -> tuple[int, dict[str, str]] | None:
    chunks = [chunk.strip() for chunk in value.split("|")]
    if len(chunks) < 2 or not chunks[0].isdigit():
        return None
    values: dict[str, str] = {}
    aliases = {
        "номер": "flight_number",
        "рейс": "flight_number",
        "отправление": "departure_airport",
        "прибытие": "arrival_airport",
        "дата": "scheduled_at",
        "время": "scheduled_at",
        "время прибытия": "arrival_at",
        "прилёт": "arrival_at",
        "прибытие время": "arrival_at",
        "статус": "status",
        "терминал": "terminal",
        "гейт": "gate",
    }
    for chunk in chunks[1:]:
        if "=" not in chunk:
            continue
        key, item = [part.strip() for part in chunk.split("=", 1)]
        key = aliases.get(key.lower(), key.lower())
        if key in {"flight_number", "departure_airport", "arrival_airport"}:
            item = item.upper()
        values[key] = item
    return int(chunks[0]), values


class Bot:
    """Application state, handlers, database, and API client."""

    def __init__(self) -> None:
        self.database = Database(DATABASE_PATH)
        owner_id = os.getenv(OWNER_ID_ENV, "").strip()
        if not owner_id.lstrip("-").isdigit() or int(owner_id) <= 0:
            raise RuntimeError(
                f"Переменная {OWNER_ID_ENV} должна содержать числовой Telegram ID"
            )
        self.owner_id = int(owner_id)
        self.database.ensure_owner(self.owner_id)
        self.lufthansa = LufthansaClient(
            safe_secret(LUFTHANSA_ID_ENV),
            safe_secret(LUFTHANSA_SECRET_ENV),
        )
        self.weather = WeatherClient()
        self.admin_code = safe_secret(ADMIN_CODE_ENV)

    def cancel_airport_times_job(self, user_id: int) -> None:
        application = getattr(self, "application", None)
        if not application or not application.job_queue:
            return
        for job in application.job_queue.get_jobs_by_name(
            f"airport-times:{user_id}"
        ):
            job.schedule_removal()

    async def airport_times_message(
        self, user_id: int, chat_id: int, message_id: int
    ) -> None:
        language = self.database.user_language(user_id) or DEFAULT_LANGUAGE
        text = translate_text(
            format_airport_clocks(datetime.now(timezone.utc)), language
        )
        markup = translate_markup(airport_times_menu(), language)
        try:
            await self.application.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=markup,
                parse_mode="HTML",
            )
        except TelegramError as error:
            LOGGER.info(
                "Не удалось обновить часы аэропортов: %s",
                type(error).__name__,
            )

    async def refresh_airport_times(
        self, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        data = context.job.data if context.job else {}
        if not isinstance(data, dict):
            return
        await self.airport_times_message(
            int(data["user_id"]),
            int(data["chat_id"]),
            int(data["message_id"]),
        )

    async def show_airport_times(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        user = update.effective_user
        query = update.callback_query
        if not user:
            return
        self.cancel_airport_times_job(user.id)
        language = self.user_language(update)
        text = translate_text(
            format_airport_clocks(datetime.now(timezone.utc)), language
        )
        markup = translate_markup(airport_times_menu(), language)
        message = None
        if query:
            message = await query.edit_message_text(
                text, reply_markup=markup, parse_mode="HTML"
            )
        elif update.message:
            message = await update.message.reply_text(
                text, reply_markup=markup, parse_mode="HTML"
            )
        if (
            message
            and self.application.job_queue
            and message.chat
            and message.message_id
        ):
            self.application.job_queue.run_repeating(
                self.refresh_airport_times,
                interval=60,
                first=60,
                data={
                    "user_id": user.id,
                    "chat_id": message.chat.id,
                    "message_id": message.message_id,
                },
                name=f"airport-times:{user.id}",
            )

    async def show_weather_menu(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        context.user_data.clear()
        await self.reply(
            update,
            "🌦 <b>Погода в аэропортах</b>\n\n"
            "Выберите аэропорт. Погода указана ориентировочно для "
            "представительной точки аэропорта и обновляется при каждом запросе.",
            weather_menu(),
        )

    async def show_weather(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        airport: str,
    ) -> None:
        context.user_data.clear()
        normalized = airport.strip().upper()
        location = WEATHER_LOCATIONS.get(normalized)
        airport_info = AIRPORTS_BY_CODE.get(normalized)
        if not location or not airport_info:
            await self.reply(
                update,
                "Для этого аэропорта пока нет данных о погоде.",
                weather_menu(),
            )
            return

        await self.reply(update, "Запрашиваю актуальную погоду…")
        try:
            current = await self.weather.current_weather(normalized)
        except (httpx.HTTPError, RuntimeError, ValueError) as error:
            LOGGER.warning(
                "Ошибка запроса погоды для %s: %s",
                normalized,
                type(error).__name__,
            )
            await self.reply(
                update,
                "Погодный сервис временно недоступен. "
                "Выберите аэропорт и попробуйте ещё раз.",
                weather_menu(normalized),
            )
            return

        temperature = current.get("temperature_2m")
        temperature = (
            current.get("temperature") if temperature is None else temperature
        )
        feels_like = current.get("apparent_temperature")
        wind_speed = current.get("wind_speed_10m")
        wind_speed = (
            current.get("windspeed") if wind_speed is None else wind_speed
        )
        humidity = current.get("relative_humidity_2m")
        measured_at = current.get("time")
        weather_code = current.get("weather_code")
        weather_code = (
            current.get("weathercode") if weather_code is None else weather_code
        )
        name = escape(str(airport_info["name"]))
        code = escape(normalized)
        text = (
            f"🌦 <b>Погода: {name} ({code})</b>\n\n"
            f"🌡 Температура: <b>{format_value(temperature)} °C</b>\n"
            f"🥶 Ощущается как: <b>{format_value(feels_like)} °C</b>\n"
            f"☁️ Условия: <b>{weather_description(weather_code)}</b>\n"
            f"💧 Влажность: <b>{format_value(humidity)}%</b>\n"
            f"💨 Ветер: <b>{format_value(wind_speed)} км/ч</b>"
        )
        if measured_at:
            text += f"\n🕒 Обновлено: <b>{escape(str(measured_at))}</b>"
        text += "\n\nИсточник: Open-Meteo"
        await self.reply(update, text, weather_menu(normalized))

    async def remember_user(self, update: Update) -> None:
        user = update.effective_user
        if user:
            self.database.upsert_user(user.id, user.username, user.first_name)

    def user_language(self, update: Update) -> str:
        user = update.effective_user
        if not user:
            return DEFAULT_LANGUAGE
        return self.database.user_language(user.id) or DEFAULT_LANGUAGE

    async def reply(
        self,
        update: Update,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
        parse_mode: str | None = "HTML",
    ) -> None:
        language = self.user_language(update)
        text = translate_text(text, language)
        reply_markup = translate_markup(reply_markup, language)
        if update.callback_query:
            await update.callback_query.edit_message_text(
                text, reply_markup=reply_markup, parse_mode=parse_mode
            )
        elif update.message:
            await update.message.reply_text(
                text, reply_markup=reply_markup, parse_mode=parse_mode
            )

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.remember_user(update)
        context.user_data.clear()
        user = update.effective_user
        if user and not self.database.user_language(user.id):
            await self.reply(
                update,
                "🌐 <b>Выберите язык / Оберіть мову / Choose a language</b>",
                language_menu(),
            )
            return
        await self.reply(
            update,
            "Добро пожаловать в <b>Lufthansa Airlines</b>!\n\n"
            "Выберите нужный раздел в меню:",
            main_menu(),
        )

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        await self.reply(
            update,
            "Здесь можно находить рейсы Lufthansa, смотреть вылеты и прилёты "
            "по аэропорту, а также получать уведомления об изменениях "
            "рейсов, добавленных администраторами.",
            back_menu(),
        )

    async def show_available_flights(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show only flights created in the bot's administrator panel."""
        context.user_data.clear()
        flights = self.database.available_flights()
        if not flights:
            await self.reply(
                update,
                "Рейсы не найдены в ближайшее время.",
                back_menu(),
            )
            return
        await self.reply(
            update,
            "<b>Доступные рейсы Lufthansa Airlines</b>\n\n"
            "Выберите рейс, чтобы получить билет и зарегистрироваться на посадку:",
            available_flights_menu(flights),
        )

    async def show_flight_board(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        direction: str,
    ) -> None:
        context.user_data.clear()
        flights = [
            flight
            for flight in self.database.flights()
            if is_visible_on_flight_board(str(flight["status"]), direction)
        ]
        title = "Вылеты" if direction == "departures" else "Прилёты"
        if not flights:
            await self.reply(
                update,
                f"Сейчас нет рейсов в разделе «{title}».",
                back_menu(),
            )
            return
        await self.reply(
            update,
            f"<b>{title}</b>\n\n"
            "Выберите рейс, чтобы посмотреть его текущее состояние:",
            flight_board_menu(flights),
        )

    async def show_flight_information(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        context.user_data.clear()
        flights = self.database.flights()
        if not flights:
            await self.reply(
                update,
                "В информации пока нет добавленных рейсов.",
                back_menu(),
            )
            return
        await self.reply(
            update,
            "<b>Информация о рейсах</b>\n\n"
            "Здесь отображаются все добавленные рейсы: запланированные, "
            "ожидающие, задержанные, вылетевшие, находящиеся в полёте и "
            "прибывшие.",
            flight_information_menu(flights),
        )

    async def show_local_flight_information(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        flight_id: int,
    ) -> None:
        del context
        flight = self.database.flight(flight_id)
        if not flight:
            await self.reply(
                update,
                "Этот рейс больше не найден.",
                back_menu(),
            )
            return
        weather_buttons = [
            InlineKeyboardButton(label, callback_data=f"weather:{airport}")
            for label, airport in (
                ("🌦 Погода вылета", str(flight["departure_airport"])),
                ("🌦 Погода прилёта", str(flight["arrival_airport"])),
            )
            if airport in WEATHER_LOCATIONS
        ]
        flight_actions = [
            [
                InlineKeyboardButton("🛫 Вылеты", callback_data="departures"),
                InlineKeyboardButton("🛬 Прилёты", callback_data="arrivals"),
            ],
        ]
        if weather_buttons:
            flight_actions.append(weather_buttons)
        flight_actions.extend(
            [
                [
                    InlineKeyboardButton(
                        "ℹ️ Все рейсы", callback_data="flight_info"
                    )
                ],
                [InlineKeyboardButton("◀️ Главное меню", callback_data="back")],
            ]
        )
        await self.reply(
            update,
            "<b>Текущее состояние рейса</b>\n\n"
            + format_local_flight_information(flight),
            InlineKeyboardMarkup(flight_actions),
        )

    async def show_user_flight(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        flight_id: int,
    ) -> None:
        del context
        user = update.effective_user
        flight = self.database.flight(flight_id)
        if not user or not flight:
            await self.reply(
                update,
                "Этот рейс больше недоступен. Рейсы не найдены в ближайшее время.",
                back_menu(),
            )
            return
        if "отмен" in str(flight["status"]).lower():
            await self.reply(
                update,
                "Этот рейс отменён и недоступен для регистрации.",
                ticket_menu(),
            )
            return
        booking, is_new = self.database.create_or_get_booking(user.id, flight_id)
        if not booking:
            await self.reply(update, "Не удалось оформить билет. Попробуйте ещё раз.", back_menu())
            return
        registration_message = (
            "Вы зарегистрировались на рейс, ожидайте начало посадки на рейс."
            if is_new
            else "Вы уже зарегистрированы на этот рейс. Сохраните данные билета."
        )
        await self.reply(
            update,
            format_ticket(flight, booking)
            + "\n\n"
            + registration_message,
            ticket_menu(),
        )

    async def user_id(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        del context
        user = update.effective_user
        if user:
            await self.reply(
                update,
                f"Ваш Telegram ID: <code>{user.id}</code>",
                back_menu(),
            )

    async def back(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        context.user_data.clear()
        await self.reply(update, "Главное меню Lufthansa Airlines:", main_menu())

    async def ask_flight(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        context.user_data["state"] = "flight"
        await self.reply(
            update,
            "Введите номер рейса, например <b>LH123</b>.",
            back_menu(),
        )

    async def ask_airport(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        direction = context.user_data.get("direction", "departures")
        context.user_data["state"] = "airport"
        title = "вылетающие" if direction == "departures" else "прибывающие"
        await self.reply(
            update,
            f"Введите трёхбуквенный код аэропорта для поиска {title} рейсов, "
            "например <b>FRA</b>.",
            back_menu(),
        )

    async def get_flight(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        value = str(context.user_data.pop("pending_value", "")).strip()
        flight_number = normalize_flight_number(value)
        if not flight_number:
            await self.reply(
                update,
                "Похоже, номер рейса указан неверно. Проверьте формат, "
                "например <b>LH123</b>.",
                back_menu(),
            )
            return
        await self.reply(update, "Запрашиваю данные Lufthansa Open API…")
        try:
            payload = await self.lufthansa.flight_status(flight_number)
            flights = extract_api_flights(payload)
            if not flights:
                await self.reply(
                    update,
                    f"Рейс <b>{flight_number}</b> не найден на сегодня. "
                    "Проверьте номер или попробуйте позже.",
                    back_menu(),
                )
                return
            text = "\n\n".join(api_flight_text(item) for item in flights[:5])
            await self.reply(update, text, back_menu())
        except (httpx.HTTPError, RuntimeError, ValueError) as error:
            LOGGER.warning("Ошибка запроса статуса рейса: %s", type(error).__name__)
            await self.reply(
                update,
                "Сервис Lufthansa временно недоступен. Попробуйте ещё раз "
                "через несколько минут.",
                back_menu(),
            )

    async def get_airport_flights(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        value = str(context.user_data.pop("pending_value", "")).strip()
        airport = normalize_airport(value)
        direction = context.user_data.get("direction", "departures")
        if not airport:
            await self.reply(
                update,
                "Нужен трёхбуквенный код аэропорта, например <b>FRA</b>.",
                back_menu(),
            )
            return
        await self.reply(update, "Запрашиваю расписание Lufthansa…")
        local_flights = self.database.active_flights_by_airport(airport, direction)
        api_flights: list[dict[str, Any]] = []
        api_error = False
        if airport not in AIRPORTS_BY_CODE:
            try:
                payload = await self.lufthansa.airport_flights(airport, direction)
                api_flights = extract_api_flights(payload)
            except (httpx.HTTPError, RuntimeError, ValueError) as error:
                api_error = True
                LOGGER.warning("Ошибка запроса аэропорта: %s", type(error).__name__)

        label = "вылетов" if direction == "departures" else "прилётов"
        if not api_flights and not local_flights:
            if api_error:
                await self.reply(
                    update,
                    "Не удалось получить расписание. Lufthansa API временно "
                    "недоступен, попробуйте позже.",
                    back_menu(),
                )
            else:
                await self.reply(
                    update,
                    f"Для аэропорта <b>{airport}</b> расписание {label} не найдено.",
                    back_menu(),
                )
            return

        sections = [f"<b>{airport}: {label} на сегодня</b>"]
        if api_flights:
            sections.append(
                "<b>Рейсы Lufthansa</b>\n\n"
                + "\n\n".join(api_flight_text(item) for item in api_flights[:10])
            )
        if local_flights:
            sections.append(
                "<b>Активные рейсы, добавленные администратором</b>\n\n"
                + "\n\n".join(format_local_flight(flight) for flight in local_flights)
            )
        if api_error:
            sections.append(
                "ℹ️ Lufthansa API временно недоступен. Показаны активные "
                "рейсы, добавленные администратором."
            )
        await self.reply(update, "\n\n".join(sections), back_menu())

    def admin_row(self, user_id: int) -> sqlite3.Row | None:
        return self.database.admin(user_id)

    async def admin_entry(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        user = update.effective_user
        if not user:
            return
        admin = self.admin_row(user.id)
        if user.id == self.owner_id or admin:
            await self.show_admin_panel(update)
            return
        context.user_data["state"] = "admin_code"
        await self.reply(
            update,
            "Панель открыта для всех, но для подтверждения введите "
            "код администратора.",
            back_menu(),
        )

    async def show_admin_panel(self, update: Update) -> None:
        user = update.effective_user
        admin = self.admin_row(user.id) if user else None
        is_owner = bool(admin and admin["is_owner"])
        await self.reply(
            update,
            "🔐 <b>Панель администратора Lufthansa Airlines</b>\n\n"
            "Администраторы могут добавлять, редактировать, отменять и "
            "удалять рейсы. Подписанные пользователи получат уведомления "
            "об изменениях.",
            admin_menu(is_owner),
        )

    async def check_admin_code(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        value = str(context.user_data.pop("pending_value", "")).strip()
        user = update.effective_user
        if not user:
            return
        if not hmac.compare_digest(value, self.admin_code):
            await self.reply(
                update,
                "Код неверный. Проверьте его и попробуйте снова.",
                back_menu(),
            )
            return
        self.database.add_admin(
            user.id,
            user.username,
            is_owner=user.id == self.owner_id,
        )
        role = "главный администратор" if user.id == self.owner_id else "администратор"
        await self.reply(
            update,
            f"Доступ подтверждён. Вы добавлены как <b>{role}</b>.",
        )
        await self.show_admin_panel(update)

    def owner(self, user_id: int) -> bool:
        return user_id == self.owner_id

    def is_admin(self, user_id: int) -> bool:
        return user_id == self.owner_id or self.admin_row(user_id) is not None

    async def require_admin(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> bool:
        user = update.effective_user
        if user and self.is_admin(user.id):
            return True
        context.user_data.clear()
        await self.reply(
            update,
            "Сначала откройте панель и подтвердите доступ кодом администратора.",
            back_menu(),
        )
        return False

    async def admin_add_flight(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not await self.require_admin(update, context):
            return
        context.user_data["state"] = "add_flight"
        await self.reply(
            update,
            "Введите данные рейса одной строкой:\n"
            f"{LOCAL_FLIGHT_FORMAT}\n\n"
            "Дополнительно можно добавить терминал и гейт через `|`.\n"
            "Для вымышленных аэропортов доступны коды: "
            "`GRC` Greater Rockford, `ORJ` Orenji, `CYP` Cyprus, "
            "`IZO` Izolirani, `GRV` Grindavik, `SOU` Sauthemptona. "
            "Можно вводить и полное название.\n"
            "Статус «Планируется» оставляет рейс только в списке управления. "
            "Для разделов «Вылеты» и «Прилёты» позже укажите статус "
            "«В полёте».",
            back_menu(),
        )

    async def admin_edit_flight(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not await self.require_admin(update, context):
            return
        context.user_data["state"] = "edit_flight"
        await self.reply(
            update,
            "Введите ID рейса и поля для изменения:\n"
            "`ID | статус=Задержан | время прибытия=2026-08-20 11:00`\n\n"
            "Поля: номер, отправление, прибытие, дата, время прибытия, "
            "статус, терминал, гейт.\n"
            "Для отправления и прибытия можно указывать любые разные коды "
            "из 3 латинских букв.\n"
            "Примеры статусов по кругу:\n"
            "• `Вылетел` — вылетел из аэропорта отправления, в полёте;\n"
            "• `В полёте` — показывается в вылетах и прилётах;\n"
            "• `На посадке` — на посадке в аэропорту прибытия;\n"
            "• `Сел` или `Прилетел` — находится в аэропорту прибытия.\n"
            "После посадки следующий статус `Вылетел` автоматически "
            "переключит рейс на обратный маршрут.",
            back_menu(),
        )

    async def admin_cancel_flight(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not await self.require_admin(update, context):
            return
        context.user_data["state"] = "cancel_flight"
        await self.reply(
            update,
            "Введите ID рейса, который нужно отменить.",
            back_menu(),
        )

    async def admin_delete_flight(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not await self.require_admin(update, context):
            return
        context.user_data["state"] = "delete_flight"
        await self.reply(
            update,
            "Введите ID рейса, который нужно удалить.",
            back_menu(),
        )

    async def admin_list_flights(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not await self.require_admin(update, context):
            return
        flights = self.database.flights()
        if not flights:
            text = "Рейсы не найдены в ближайшее время."
        else:
            text = "<b>Управляемые рейсы</b>\n\n" + "\n\n".join(
                format_local_flight(flight) for flight in flights
            )
        await self.reply(update, text, admin_menu(self.owner(update.effective_user.id)))

    async def admin_broadcast(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not await self.require_admin(update, context):
            return
        flights = self.database.flights()
        if not flights:
            await self.reply(
                update,
                "Рейсы не найдены в ближайшее время.",
                admin_menu(self.owner(update.effective_user.id)),
            )
            return
        await self.reply(
            update,
            "Выберите рейс. Сообщение получат только пользователи, "
            "зарегистрированные на выбранный рейс:",
            admin_broadcast_menu(flights),
        )

    async def select_broadcast_flight(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        flight_id: int,
    ) -> None:
        if not await self.require_admin(update, context):
            return
        flight = self.database.flight(flight_id)
        if not flight:
            await self.reply(
                update,
                "Рейс не найден. Рейсы не найдены в ближайшее время.",
                admin_menu(self.owner(update.effective_user.id)),
            )
            return
        context.user_data["state"] = "admin_broadcast"
        context.user_data["broadcast_flight_id"] = flight_id
        await self.reply(
            update,
            "Введите сообщение для пассажиров рейса:\n\n"
            + format_local_flight(flight),
            back_menu(),
        )

    async def send_flight_broadcast(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not await self.require_admin(update, context):
            return
        value = str(context.user_data.pop("pending_value", "")).strip()
        flight_id = context.user_data.pop("broadcast_flight_id", None)
        user = update.effective_user
        if not user or not value or not isinstance(flight_id, int):
            await self.reply(update, "Сообщение не может быть пустым.", back_menu())
            return
        flight = self.database.flight(flight_id)
        if not flight:
            await self.reply(
                update,
                "Рейс не найден. Рейсы не найдены в ближайшее время.",
                admin_menu(self.owner(user.id)),
            )
            return
        recipient_ids = self.database.booking_user_ids(flight_id)
        if not recipient_ids:
            await self.reply(
                update,
                "На этот рейс ещё никто не зарегистрировался, рассылка не отправлена.",
                admin_menu(self.owner(user.id)),
            )
            return
        sent = 0
        for user_id in recipient_ids:
            try:
                recipient_language = (
                    self.database.user_language(user_id) or DEFAULT_LANGUAGE
                )
                await self.application.bot.send_message(
                    user_id,
                    translate_text(
                        f"📣 Сообщение по рейсу {flight['flight_number']}:\n\n{value}",
                        recipient_language,
                    ),
                    parse_mode=None,
                )
                sent += 1
                await asyncio.sleep(0.04)
            except TelegramError as error:
                LOGGER.info(
                    "Не удалось отправить сообщение пассажиру %s: %s",
                    user_id,
                    type(error).__name__,
                )
        await self.reply(
            update,
            f"Сообщение отправлено пассажирам рейса: {sent}.",
            admin_menu(self.owner(user.id)),
        )

    async def admin_manage(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        del context
        user = update.effective_user
        if not user or not self.owner(user.id):
            await self.reply(
                update,
                "Управлять списком администраторов может только главный администратор.",
                back_menu(),
            )
            return
        admins = self.database.admins()
        lines = ["<b>Администраторы</b>"]
        for admin in admins:
            role = "главный" if admin["is_owner"] else "обычный"
            name = f"@{admin['username']}" if admin["username"] else "без username"
            lines.append(f"• <code>{admin['user_id']}</code> — {name} ({role})")
        await self.reply(
            update,
            "\n".join(lines),
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "➕ Добавить администратора",
                            callback_data="admin_add_admin",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "🗑 Удалить администратора",
                            callback_data="admin_remove_admin",
                        )
                    ],
                    [InlineKeyboardButton("◀️ Назад", callback_data="admin")],
                ]
            ),
        )

    async def admin_add_admin(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        user = update.effective_user
        if not user or not self.owner(user.id):
            await self.reply(update, "Недостаточно прав.", back_menu())
            return
        context.user_data["state"] = "add_admin"
        await self.reply(
            update,
            "Введите Telegram ID пользователя, которого нужно добавить "
            "администратором. Узнать ID можно командой /id.",
            back_menu(),
        )

    async def admin_remove_admin(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        user = update.effective_user
        if not user or not self.owner(user.id):
            await self.reply(update, "Недостаточно прав.", back_menu())
            return
        context.user_data["state"] = "remove_admin"
        await self.reply(
            update,
            "Введите Telegram ID администратора, которого нужно удалить.",
            back_menu(),
        )

    async def broadcast(self, text: str, recipient_ids: list[int]) -> None:
        application = getattr(self, "application", None)
        if not application:
            return
        for user_id in recipient_ids:
            try:
                await application.bot.send_message(user_id, text, parse_mode="HTML")
                await asyncio.sleep(0.04)
            except TelegramError as error:
                LOGGER.info(
                    "Не удалось отправить уведомление пользователю %s: %s",
                    user_id,
                    type(error).__name__,
                )

    async def process_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await self.remember_user(update)
        state = context.user_data.get("state")
        text = (update.message.text if update.message else "").strip()
        if not text:
            return
        context.user_data["pending_value"] = text

        if state == "flight":
            await self.get_flight(update, context)
        elif state == "airport":
            await self.get_airport_flights(update, context)
        elif state == "admin_code":
            await self.check_admin_code(update, context)
        elif state == "admin_broadcast":
            await self.send_flight_broadcast(update, context)
        elif state == "add_flight":
            await self.create_managed_flight(update, context)
        elif state == "edit_flight":
            await self.edit_managed_flight(update, context)
        elif state == "cancel_flight":
            await self.cancel_managed_flight(update, context)
        elif state == "delete_flight":
            await self.delete_managed_flight(update, context)
        elif state == "add_admin":
            await self.add_managed_admin(update, context)
        elif state == "remove_admin":
            await self.remove_managed_admin(update, context)
        else:
            await self.reply(update, "Выберите раздел в главном меню.", main_menu())

    async def create_managed_flight(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not await self.require_admin(update, context):
            return
        value = str(context.user_data.pop("pending_value", ""))
        parsed = parse_local_flight(value)
        user = update.effective_user
        if not parsed or not user:
            await self.reply(
                update,
                f"Не удалось разобрать данные рейса.\n{LOCAL_FLIGHT_FORMAT}",
                back_menu(),
            )
            return
        (
            flight_number,
            departure,
            arrival,
            scheduled,
            arrival_at,
            status,
            terminal,
            gate,
        ) = parsed
        flight_id = self.database.create_flight(
            flight_number,
            departure,
            arrival,
            scheduled,
            arrival_at,
            status,
            terminal,
            gate,
            user.id,
        )
        flight = self.database.flight(flight_id)
        await self.reply(
            update,
            "Рейс добавлен. Подписанные пользователи получили уведомление.",
            admin_menu(self.owner(user.id)),
        )

    async def edit_managed_flight(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not await self.require_admin(update, context):
            return
        value = str(context.user_data.pop("pending_value", ""))
        parsed = parse_edit_values(value)
        user = update.effective_user
        if not parsed or not user:
            await self.reply(
                update,
                "Формат изменения неверный. Пример: "
                "`3 | статус=Задержан | гейт=A12`.",
                back_menu(),
            )
            return
        flight_id, values = parsed
        if "flight_number" in values and not normalize_flight_number(values["flight_number"]):
            await self.reply(update, "Номер рейса указан неверно.", back_menu())
            return
        for key in ("departure_airport", "arrival_airport"):
            if key in values and not normalize_airport(values[key]):
                await self.reply(
                    update,
                    "Код аэропорта должен состоять из 3 латинских букв.",
                    back_menu(),
                )
                return
        current_flight = self.database.flight(flight_id)
        if current_flight:
            departure = values.get(
                "departure_airport", str(current_flight["departure_airport"])
            )
            arrival = values.get(
                "arrival_airport", str(current_flight["arrival_airport"])
            )
            if departure == arrival:
                await self.reply(
                    update,
                    "Аэропорты отправления и прибытия должны быть разными.",
                    back_menu(),
                )
                return
        if not self.database.update_flight(flight_id, values):
            await self.reply(update, "Рейс не найден или нет полей для изменения.", back_menu())
            return
        flight = self.database.flight(flight_id)
        if flight:
            await self.broadcast(
                "🔄 <b>Рейс Lufthansa Airlines изменён</b>\n\n"
                + format_local_flight(flight),
                self.database.booking_user_ids(flight_id),
            )
        await self.reply(
            update,
            "Рейс изменён. Подписанные пользователи получили уведомление.",
            admin_menu(self.owner(user.id)),
        )

    async def cancel_managed_flight(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not await self.require_admin(update, context):
            return
        value = str(context.user_data.pop("pending_value", ""))
        user = update.effective_user
        if not user or not value.isdigit():
            await self.reply(update, "Введите числовой ID рейса.", back_menu())
            return
        flight_id = int(value)
        if not self.database.update_flight(flight_id, {"status": "Отменён"}):
            await self.reply(update, "Рейс с таким ID не найден.", back_menu())
            return
        flight = self.database.flight(flight_id)
        if flight:
            await self.broadcast(
                "⚠️ <b>Рейс Lufthansa Airlines отменён</b>\n\n"
                + format_local_flight(flight),
                self.database.booking_user_ids(flight_id),
            )
        await self.reply(
            update,
            "Рейс отменён. Подписанные пользователи получили уведомление.",
            admin_menu(self.owner(user.id)),
        )

    async def delete_managed_flight(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not await self.require_admin(update, context):
            return
        value = str(context.user_data.pop("pending_value", ""))
        user = update.effective_user
        if not user or not value.isdigit():
            await self.reply(update, "Введите числовой ID рейса.", back_menu())
            return
        flight_id = int(value)
        flight = self.database.flight(flight_id)
        recipient_ids = self.database.booking_user_ids(flight_id)
        if not flight or not self.database.delete_flight(flight_id):
            await self.reply(update, "Рейс с таким ID не найден.", back_menu())
            return
        await self.broadcast(
            "🗑 <b>Рейс Lufthansa Airlines удалён из расписания</b>\n\n"
            + format_local_flight(flight),
            recipient_ids,
        )
        await self.reply(
            update,
            "Рейс удалён. Подписанные пользователи получили уведомление.",
            admin_menu(self.owner(user.id)),
        )

    async def add_managed_admin(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        value = str(context.user_data.pop("pending_value", ""))
        user = update.effective_user
        if not user or not self.owner(user.id):
            await self.reply(update, "Недостаточно прав.", back_menu())
            return
        if not value.lstrip("-").isdigit():
            await self.reply(update, "Telegram ID должен быть числом.", back_menu())
            return
        target_id = int(value)
        self.database.add_admin(target_id, None)
        await self.reply(
            update,
            f"Пользователь <code>{target_id}</code> добавлен администратором.",
            admin_menu(True),
        )

    async def remove_managed_admin(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        value = str(context.user_data.pop("pending_value", ""))
        user = update.effective_user
        if not user or not self.owner(user.id):
            await self.reply(update, "Недостаточно прав.", back_menu())
            return
        if not value.lstrip("-").isdigit():
            await self.reply(update, "Telegram ID должен быть числом.", back_menu())
            return
        removed = self.database.remove_admin(int(value))
        result = "Администратор удалён." if removed else "Такой обычный администратор не найден."
        await self.reply(update, result, admin_menu(True))

    async def callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        if not query:
            return
        await query.answer()
        await self.remember_user(update)
        action = query.data or ""
        user = update.effective_user
        if user and action not in {"airport_times", "airport_times_refresh"}:
            self.cancel_airport_times_job(user.id)
        if action == "language":
            await self.reply(
                update,
                "🌐 <b>Выберите язык / Оберіть мову / Choose a language</b>",
                language_menu(),
            )
        elif action.startswith("language:"):
            language = action.split(":", 1)[1]
            user = update.effective_user
            if user and language in SUPPORTED_LANGUAGES:
                self.database.set_user_language(user.id, language)
            await self.reply(
                update,
                "✅ Язык сохранён.\n\nДобро пожаловать в <b>Lufthansa Airlines</b>!\n\n"
                "Выберите нужный раздел в меню:",
                main_menu(),
            )
        elif action in {"airport_times", "airport_times_refresh"}:
            await self.show_airport_times(update, context)
        elif action == "weather":
            await self.show_weather_menu(update, context)
        elif action.startswith("weather_refresh:"):
            await self.show_weather(
                update,
                context,
                action.split(":", 1)[1],
            )
        elif action.startswith("weather:"):
            await self.show_weather(
                update,
                context,
                action.split(":", 1)[1],
            )
        elif action == "back":
            await self.back(update, context)
        elif action == "help":
            await self.help(update, context)
        elif action in {"find_flight", "available_flights"}:
            await self.show_available_flights(update, context)
        elif action == "flight_info":
            await self.show_flight_information(update, context)
        elif action == "flight_info_search":
            await self.ask_flight(update, context)
        elif action.startswith("info_local_flight:"):
            try:
                flight_id = int(action.split(":", 1)[1])
            except ValueError:
                await self.show_flight_information(update, context)
            else:
                await self.show_local_flight_information(update, context, flight_id)
        elif action.startswith("user_flight:"):
            try:
                flight_id = int(action.split(":", 1)[1])
            except ValueError:
                await self.show_available_flights(update, context)
            else:
                await self.show_user_flight(update, context, flight_id)
        elif action in {"departures", "arrivals"}:
            await self.show_flight_board(update, context, action)
        elif action == "admin":
            await self.admin_entry(update, context)
        elif action == "admin_add_flight":
            await self.admin_add_flight(update, context)
        elif action == "admin_edit_flight":
            await self.admin_edit_flight(update, context)
        elif action == "admin_cancel_flight":
            await self.admin_cancel_flight(update, context)
        elif action == "admin_delete_flight":
            await self.admin_delete_flight(update, context)
        elif action == "admin_list_flights":
            await self.admin_list_flights(update, context)
        elif action == "admin_broadcast":
            await self.admin_broadcast(update, context)
        elif action.startswith("admin_broadcast_flight:"):
            try:
                flight_id = int(action.split(":", 1)[1])
            except ValueError:
                await self.admin_broadcast(update, context)
            else:
                await self.select_broadcast_flight(update, context, flight_id)
        elif action == "admin_manage":
            await self.admin_manage(update, context)
        elif action == "admin_add_admin":
            await self.admin_add_admin(update, context)
        elif action == "admin_remove_admin":
            await self.admin_remove_admin(update, context)

    async def error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        LOGGER.error("Непредвиденная ошибка: %s", type(context.error).__name__)

    def application(self) -> Application:
        application = Application.builder().token(safe_secret(TOKEN_ENV)).build()
        application.add_handler(CommandHandler("start", self.start))
        application.add_handler(CommandHandler("help", self.help))
        application.add_handler(CommandHandler("id", self.user_id))
        application.add_handler(CallbackQueryHandler(self.callback))
        application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.process_text)
        )
        application.add_error_handler(self.error)
        self.application = application
        return application


def configure_logging() -> None:
    logging.basicConfig(
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        level=logging.INFO,
    )
    # HTTP request logs may contain authorization URLs or headers. Keep them
    # quiet and log only the bot's own safe diagnostic messages.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def main() -> None:
    configure_logging()
    bot = Bot()
    bot.application().run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()