"""
Centralized Settings Management
All configuration values in one place
"""

import os
from dotenv import load_dotenv
from typing import Optional

# Load .env file
load_dotenv()


class Settings:
    """Application settings loaded from environment variables"""
    
    # McTime API
    MCTIME_API_KEY: str = os.getenv('MCTIME_API_KEY', '')
    MCTIME_BASE_URL: str = os.getenv('MCTIME_BASE_URL', 'https://mctime.com/api/v2/auth')
    
    # SMTP / Email
    EMAIL_METHOD: str = os.getenv('EMAIL_METHOD', 'smtp').lower()
    SMTP_SERVER: str = os.getenv('SMTP_SERVER', '')
    SMTP_PORT: int = int(os.getenv('SMTP_PORT', '587'))
    SMTP_USERNAME: str = os.getenv('SMTP_USERNAME', '')
    SMTP_PASSWORD: str = os.getenv('SMTP_PASSWORD', '')
    SENDER_EMAIL: str = os.getenv('SENDER_EMAIL', '')
    USE_TLS: bool = os.getenv('USE_TLS', 'true').lower() == 'true'
    # Resend API (Railway-kompatibel, kein SMTP nötig)
    RESEND_API_KEY: str = os.getenv('RESEND_API_KEY', '')

    # Branding
    APP_COMPANY_NAME: str = os.getenv('APP_COMPANY_NAME', 'McTime')
    
    # Flask
    FLASK_SECRET_KEY: str = os.getenv('FLASK_SECRET_KEY', 'dev-secret-key-change-in-production')
    FLASK_DEBUG: bool = os.getenv('FLASK_DEBUG', 'false').lower() == 'true'
    FLASK_HOST: str = os.getenv('FLASK_HOST', '127.0.0.1')
    FLASK_PORT: int = int(os.getenv('FLASK_PORT', '5000'))
    
    # Webhook
    WEBHOOK_URL: str = os.getenv('WEBHOOK_URL', '')
    WEBHOOK_TOKEN: str = os.getenv('WEBHOOK_TOKEN', '')
    
    @classmethod
    def validate(cls) -> dict:
        """Validate that required settings are configured"""
        issues = []
        
        if not cls.MCTIME_API_KEY:
            issues.append('MCTIME_API_KEY is not set')
        
        return {
            'valid': len(issues) == 0,
            'issues': issues
        }
    
    @classmethod
    def is_email_configured(cls) -> bool:
        """Check if email settings are configured"""
        return all([
            cls.SMTP_SERVER,
            cls.SMTP_PORT,
            cls.SMTP_USERNAME,
            cls.SMTP_PASSWORD,
            cls.SENDER_EMAIL
        ])


# Singleton instance
_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """Get settings singleton"""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
