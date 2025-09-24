from datetime import datetime
import pytz

def hora_houston():
    """Devuelve la hora actual de Houston en formato 12 horas AM/PM"""
    houston_tz = pytz.timezone("America/Chicago")
    return datetime.now(houston_tz).strftime("%I:%M %p")
