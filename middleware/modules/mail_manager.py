"""
Mail Manager - Modul für Mailversand-Funktionalität
"""

import os
import smtplib
import json
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Dict, List, Optional
import requests as http_requests


class MailManager:
    """
    Verwaltet E-Mail-Versand für Zeitberichte
    """
    
    def __init__(self, request_handler=None):
        """
        Initialisiert Mail Manager
        
        Args:
            request_handler: RequestHandler (optional, für API-basierte Mail-Dienste)
        """
        self.request_handler = request_handler
        
        # E-Mail-Methode: "smtp" oder "resend"
        self.email_method = os.getenv("EMAIL_METHOD", "smtp").lower()

        # SMTP-Konfiguration aus Umgebungsvariablen
        self.smtp_server = os.getenv("SMTP_SERVER")
        self.smtp_port = int(os.getenv("SMTP_PORT", 587))
        self.smtp_username = os.getenv("SMTP_USERNAME")
        self.smtp_password = os.getenv("SMTP_PASSWORD")
        self.sender_email = os.getenv("SENDER_EMAIL")
        self.use_tls = os.getenv("USE_TLS", "true").lower() == "true"

        # Resend-Konfiguration
        self.resend_api_key = os.getenv("RESEND_API_KEY", "")
    
    def is_configured(self) -> bool:
        """
        Prüft ob E-Mail-Versand konfiguriert ist (SMTP oder Resend).
        
        Returns:
            True wenn alle erforderlichen Variablen gesetzt sind
        """
        if self.email_method == "resend":
            return bool(self.resend_api_key and self.sender_email)
        return all([
            self.smtp_server,
            self.smtp_username,
            self.smtp_password,
            self.sender_email
        ])
    
    def send_time_report(
        self,
        employee_id: str,
        employee_name: str,
        date_from: str,
        date_to: str,
        time_manager,
        employee_manager,
        custom_email: str = None,
        custom_subject: str = None,
        attach_csv: bool = True
    ) -> Dict:
        """
        Sendet Zeitbericht per E-Mail
        
        Args:
            employee_id: ID des Mitarbeiters
            employee_name: Name des Mitarbeiters
            date_from: Startdatum
            date_to: Enddatum
            time_manager: TimeManager für Zeitdaten
            employee_manager: EmployeeManager für E-Mail-Adresse
            
        Returns:
            Dict mit Status der Versendung
        """
        # Hole E-Mail-Adresse (custom oder vom Mitarbeiter)
        if custom_email:
            employee_email = custom_email
        else:
            employee_email = employee_manager.get_employee_email(employee_id)
            if not employee_email:
                return {
                    "status": "error",
                    "message": f"Keine E-Mail-Adresse für Mitarbeiter {employee_name} gefunden"
                }
        
        # Hole Zeiteinträge
        time_entries = time_manager.get_time_entries(
            employee_id=employee_id,
            date_from=date_from,
            date_to=date_to
        )
        
        if not time_entries:
            return {
                "status": "error",
                "message": "Keine Zeiteinträge für den angegebenen Zeitraum gefunden"
            }
        
        # Erstelle E-Mail-Inhalt
        if custom_subject:
            subject = custom_subject
        else:
            subject = f"Zeiterfassung für {employee_name} ({date_from} bis {date_to})"
        html_body = self._create_report_html(
            employee_name=employee_name,
            time_entries=time_entries,
            date_from=date_from,
            date_to=date_to
        )
        
        # Erstelle CSV falls gewünscht
        csv_content = None
        if attach_csv:
            csv_content = self._create_csv_content(time_entries, employee_name)
        
        # Sende E-Mail
        success = self._send_email(
            to_email=employee_email,
            subject=subject,
            html_body=html_body,
            cc_email=None,
            csv_content=csv_content,
            csv_filename=f"zeiterfassung_{employee_name}_{date_from}_{date_to}.csv"
        )
        
        if success:
            return {
                "status": "success",
                "message": "E-Mail erfolgreich gesendet",
                "email": employee_email
            }
        else:
            return {
                "status": "error",
                "message": "Fehler beim Senden der E-Mail"
            }
    
    def _create_report_html(
        self,
        employee_name: str,
        time_entries: List[Dict],
        date_from: str,
        date_to: str
    ) -> str:
        """
        Erstellt HTML für Zeitbericht
        
        Args:
            employee_name: Name des Mitarbeiters
            time_entries: Liste der Zeiteinträge
            date_from: Startdatum
            date_to: Enddatum
            
        Returns:
            HTML-String
        """
        # Berechne Summen
        total_work_hours = sum(e.get("actual_work_hours", 0) for e in time_entries)
        total_entries = len(time_entries)
        
        html = f"""
        <html>
        <head>
            <style>
                body {{ font-family: Arial, sans-serif; }}
                table {{ border-collapse: collapse; width: 100%; }}
                th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
                th {{ background-color: #4CAF50; color: white; }}
                tr:nth-child(even) {{ background-color: #f2f2f2; }}
                .summary {{ background-color: #e7f3fe; padding: 10px; margin-bottom: 20px; }}
            </style>
        </head>
        <body>
            <h2>Zeiterfassung - {employee_name}</h2>
            
            <div class="summary">
                <p><strong>Zeitraum:</strong> {date_from} bis {date_to}</p>
                <p><strong>Gesamtanzahl Einträge:</strong> {total_entries}</p>
                <p><strong>Gesamte Arbeitsstunden:</strong> {total_work_hours:.2f}h</p>
            </div>
            
            <h3>Detaillierte Aufstellung:</h3>
            <table>
                <tr>
                    <th>Datum</th>
                    <th>Projekt</th>
                    <th>Arbeitszeit</th>
                    <th>Pausen</th>
                    <th>Effektive Stunden</th>
                </tr>
        """
        
        for entry in time_entries:
            html += f"""
                <tr>
                    <td>{entry.get('date_formatted', 'N/A')}</td>
                    <td>{entry.get('project', 'N/A')}</td>
                    <td>{entry.get('time_formatted', 'N/A')}</td>
                    <td>{entry.get('breaks_formatted', 'N/A')}</td>
                    <td>{entry.get('actual_work_hours', 0):.2f}h</td>
                </tr>
            """
        
        html += """
            </table>
            <br>
            <p><em>Diese E-Mail wurde automatisch vom McTime System generiert.</em></p>
        </body>
        </html>
        """
        
        return html
    
    def _create_csv_content(
        self,
        time_entries: List[Dict],
        employee_name: str
    ) -> str:
        """Erstellt CSV-Inhalt im korrekten Format"""
        import csv
        import io
        
        output = io.StringIO()
        
        # CSV Header entsprechend dem Original
        headers = [
            'Personalnummer', 'Vorname', 'Nachname', 'Datum', 'Type',
            'Zeit Beginn', 'Zeit Ende', 'Pause', 'Summe mit Pause', 
            'Summe ohne Pause', 'Projektnummer', 'Auftragsnummer',
            'Projekt / Gruppenname', 'Kommentar'
        ]
        
        writer = csv.writer(output, delimiter=';')
        writer.writerow(headers)
        
        # Aufteile employee_name in Vor- und Nachname
        name_parts = employee_name.split(' ', 1)
        first_name = name_parts[0] if len(name_parts) > 0 else ''
        last_name = name_parts[1] if len(name_parts) > 1 else ''
        
        for entry in time_entries:
            writer.writerow([
                entry.get('employee_id', ''),      # Personalnummer (UUID)
                first_name,                         # Vorname
                last_name,                          # Nachname
                entry.get('date_formatted', ''),    # Datum (01.10.25)
                'Arbeitszeit',                      # Type
                entry.get('time_start', ''),        # Zeit Beginn (05:00)
                entry.get('time_end', ''),          # Zeit Ende (18:00)
                f'"{entry.get("breaks_formatted", "")}"' if entry.get('breaks_formatted') else '', # Pause mit Anführungszeichen
                entry.get('total_hours_formatted', ''), # Summe mit Pause (13:00)
                entry.get('actual_hours_formatted', ''), # Summe ohne Pause (12:00)
                '',                                 # Projektnummer
                '',                                 # Auftragsnummer
                entry.get('project', ''),           # Projekt / Gruppenname
                ''                                  # Kommentar
            ])
        
        return output.getvalue()
    
    def _send_email(
        self,
        to_email: str,
        subject: str,
        html_body: str,
        cc_email: str = None,
        csv_content: str = None,
        csv_filename: str = None
    ) -> bool:
        """
        Sendet E-Mail via SMTP
        
        Args:
            to_email: Empfänger-Adresse(n), Komma-getrennt für mehrere
            subject: Betreff
            html_body: HTML-Inhalt
            cc_email: CC-Empfänger-Adresse(n), Komma-getrennt für mehrere
            csv_content: CSV-Inhalt als String
            csv_filename: Dateiname für CSV-Anhang
            
        Returns:
            True bei Erfolg
        """
        if not self.is_configured():
            if self.email_method == "resend":
                print("FEHLER: Resend nicht konfiguriert!")
                print("Bitte setze: RESEND_API_KEY, SENDER_EMAIL")
            else:
                print("FEHLER: SMTP nicht konfiguriert!")
                print("Bitte setze: SMTP_SERVER, SMTP_USERNAME, SMTP_PASSWORD, SENDER_EMAIL")
            return False

        if self.email_method == "resend":
            return self._send_via_resend(
                to_email=to_email,
                subject=subject,
                html_body=html_body,
                cc_email=cc_email,
                csv_content=csv_content,
                csv_filename=csv_filename,
            )
        
        try:
            print("=== E-MAIL VERSENDEN ===")
            print(f"SMTP Server: {self.smtp_server}:{self.smtp_port}")
            print(f"Von: {self.sender_email}")
            print(f"An: {to_email}")
            print(f"CC: {cc_email if cc_email else 'Keine'}")
            print(f"Betreff: {subject}")
            
            # Parse Empfänger-Adressen (mehrere mit Komma getrennt)
            to_list = [email.strip() for email in to_email.split(',') if email.strip()]
            cc_list = [email.strip() for email in cc_email.split(',') if email.strip()] if cc_email else []
            
            # Erstelle Nachricht
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = self.sender_email
            msg["To"] = ", ".join(to_list)
            
            # CC hinzufügen falls vorhanden
            if cc_list:
                msg["Cc"] = ", ".join(cc_list)
            
            # Alle Empfänger für send_message (To + CC)
            all_recipients = to_list + cc_list
            
            # Füge HTML hinzu
            html_part = MIMEText(html_body, "html", "utf-8")
            msg.attach(html_part)
            
            # Füge CSV-Anhang hinzu falls vorhanden
            if csv_content and csv_filename:
                from email.mime.application import MIMEApplication
                
                csv_attachment = MIMEApplication(
                    csv_content.encode('utf-8'),
                    _subtype='csv'
                )
                csv_attachment.add_header(
                    'Content-Disposition',
                    f'attachment; filename="{csv_filename}"'
                )
                msg.attach(csv_attachment)
            
            # Verbinde zu SMTP
            print(f"Verbinde zu SMTP mit TLS: {self.use_tls}")
            server = smtplib.SMTP(self.smtp_server, self.smtp_port)
            
            if self.use_tls:
                print("Aktiviere TLS...")
                server.starttls()
            
            print("Login...")
            server.login(self.smtp_username, self.smtp_password)
            
            print("Sende E-Mail...")
            server.sendmail(self.sender_email, all_recipients, msg.as_string())
            server.quit()

            print(f"[OK] E-Mail erfolgreich gesendet an {len(to_list)} Empfänger" + (f" (+ {len(cc_list)} CC)" if cc_list else ""))
            return True
            
        except smtplib.SMTPAuthenticationError as e:
            print(f"SMTP Authentifizierungsfehler: {e}")
            return False
        except smtplib.SMTPException as e:
            print(f"SMTP Fehler: {e}")
            return False
        except Exception as e:
            print(f"E-Mail Versandfehler: {e}")
            return False

    def _send_via_resend(
        self,
        to_email: str,
        subject: str,
        html_body: str,
        cc_email: str = None,
        csv_content: str = None,
        csv_filename: str = None,
    ) -> bool:
        """
        Sendet E-Mail über die Resend HTTP API (kein SMTP, funktioniert auf Railway).
        """
        try:
            print("=== E-MAIL VERSENDEN (Resend API) ===")
            print(f"Von: {self.sender_email}")
            print(f"An: {to_email}")
            print(f"CC: {cc_email if cc_email else 'Keine'}")
            print(f"Betreff: {subject}")

            to_list = [e.strip() for e in to_email.split(',') if e.strip()]
            cc_list = [e.strip() for e in cc_email.split(',') if e.strip()] if cc_email else []

            payload: dict = {
                "from": self.sender_email,
                "to": to_list,
                "subject": subject,
                "html": html_body,
            }
            if cc_list:
                payload["cc"] = cc_list

            # CSV-Anhang als base64-Attachment
            if csv_content and csv_filename:
                import base64
                encoded = base64.b64encode(csv_content.encode("utf-8")).decode("ascii")
                payload["attachments"] = [{
                    "filename": csv_filename,
                    "content": encoded,
                    "content_type": "text/csv",
                }]

            resp = http_requests.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {self.resend_api_key}",
                    "Content-Type": "application/json",
                },
                data=json.dumps(payload),
                timeout=15,
            )

            if resp.status_code in (200, 201):
                print(f"[OK] E-Mail über Resend gesendet: {resp.json().get('id', '')}")
                return True
            else:
                print(f"Resend API Fehler {resp.status_code}: {resp.text}")
                return False

        except Exception as e:
            print(f"Resend Versandfehler: {e}")
            return False

    def test_connection(self) -> Dict:
        """
        Testet E-Mail-Verbindung (SMTP oder Resend).
        
        Returns:
            Dict mit Test-Ergebnis
        """
        if not self.is_configured():
            return {
                "status": "error",
                "message": "E-Mail nicht konfiguriert (EMAIL_METHOD, SMTP_* oder RESEND_API_KEY prüfen)"
            }

        if self.email_method == "resend":
            try:
                resp = http_requests.get(
                    "https://api.resend.com/domains",
                    headers={"Authorization": f"Bearer {self.resend_api_key}"},
                    timeout=10,
                )
                if resp.status_code == 200:
                    return {"status": "success", "message": "Resend API-Key gültig"}
                return {"status": "error", "message": f"Resend API-Key ungültig ({resp.status_code})"}
            except Exception as e:
                return {"status": "error", "message": f"Resend-Verbindung fehlgeschlagen: {e}"}
        
        try:
            server = smtplib.SMTP(self.smtp_server, self.smtp_port)
            
            if self.use_tls:
                server.starttls()
            
            server.login(self.smtp_username, self.smtp_password)
            server.quit()
            
            return {
                "status": "success",
                "message": "SMTP-Verbindung erfolgreich"
            }
            
        except Exception as e:
            return {
                "status": "error",
                "message": f"SMTP-Verbindung fehlgeschlagen: {str(e)}"
            }
