import requests
import os
from datetime import datetime
from typing import Dict, List, Optional

class McTimeAPI:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://mctime.com/api/v2/auth"
        self.headers = {
            "content-type": "application/json",
            "API_KEY": api_key
        }

    @staticmethod
    def _normalize_personnel_number(value: Optional[str]) -> str:
        """Normalisiert Personalnummern auf numerische Strings."""
        if value is None:
            return ""
        digits_only = ''.join(ch for ch in str(value) if ch.isdigit())
        return digits_only
    
    def get_organizations(self) -> List[Dict]:
        """
        Get list of organizations (companies/firms)
        Gibt alle verfuegbaren Organizations fuer den API-Key zurueck.
        Optional kann mit MCTIME_ORGANIZATION_FILTER auf einen Firmennamen gefiltert werden.
        """
        url = f"{self.base_url}/organizations"
        try:
            response = requests.get(url, headers=self.headers)
            if response.status_code == 200:
                data = response.json()
                organizations = data.get("items", [{}])[0].get("data", {}).get("organizations", [])
                mapped_orgs = [
                    {"id": org.get("id"), "name": org.get("organizationName")}
                    for org in organizations
                ]

                org_filter = os.getenv("MCTIME_ORGANIZATION_FILTER", "").strip().lower()
                if org_filter:
                    mapped_orgs = [
                        org for org in mapped_orgs
                        if (org.get("name") or "").strip().lower() == org_filter
                    ]

                return mapped_orgs
            else:
                print(f"Error fetching organizations: {response.status_code}")
                return []
        except Exception as e:
            print(f"Exception in get_organizations: {e}")
            return []
    
    def _get_user_detail(self, user_id: str) -> Dict:
        """Holt Detail-Daten eines Users über /users/{id} (enthält exportIds, userEmployments etc.)"""
        url = f"{self.base_url}/users/{user_id}"
        try:
            response = requests.get(url, headers=self.headers)
            if response.status_code == 200:
                data = response.json()
                return data.get("items", [{}])[0].get("data", {})
            return {}
        except Exception:
            return {}

    def _extract_personnel_number(self, detail: Dict) -> str:
        """Extrahiert Personalnummer aus User-Detail-Daten.
        Prüft: exportIds.exportId1/2/3, userEmployments[].basicData.employeeId/employmentId"""
        # exportIds prüfen
        export_ids = detail.get("exportIds", {})
        for key in ("exportId1", "exportId2", "exportId3"):
            val = export_ids.get(key)
            if val:
                return self._normalize_personnel_number(val)

        # userEmployments prüfen
        for emp in detail.get("userEmployments", []):
            basic = emp.get("basicData", {})
            for key in ("employeeId", "employmentId"):
                val = basic.get(key)
                if val:
                    return self._normalize_personnel_number(val)

        return ""

    def get_employees(self, organization_id: Optional[str] = None, organization_name: Optional[str] = None) -> List[Dict]:
        """
        Get list of employees/users
        Hinweis: Organisations-Filter deaktiviert (API-Berechtigungen fehlen)
        Holt Personalnummern über den Detail-Endpoint /users/{id}.
        """
        url = f"{self.base_url}/users"
        params = {"roles": "employee"}
        # Organisations-Filter deaktiviert - keine API-Berechtigung
            
        try:
            response = requests.get(url, headers=self.headers, params=params)
            
            if response.status_code == 200:
                data = response.json()
                users = data.get("items", [{}])[0].get("data", {}).get("users", [])
                
                employees = []
                for user in users:
                    full_name = f"{user.get('firstName', '')} {user.get('lastName', '')}".strip()
                    if not full_name:
                        full_name = "Unknown User"

                    # Personalnummer aus Detail-Endpoint holen
                    user_id = user.get("id", "")
                    detail = self._get_user_detail(user_id) if user_id else {}
                    personnel_number = self._extract_personnel_number(detail)

                    employees.append({
                        "id": user_id,
                        "name": full_name,
                        "firstName": user.get("firstName", ""),
                        "lastName": user.get("lastName", ""),
                        "email": user.get("email"),
                        "organizationId": user.get("organizationId"),
                        "personnel_number": personnel_number
                    })
                return sorted(employees, key=lambda x: x["name"])
            else:
                print(f"Error fetching employees: {response.status_code}")
                return []
        except Exception as e:
            print(f"Exception in get_employees: {e}")
            return []
    
    def get_user_name_by_id(self, user_id: str) -> str:
        """
        Get user name by ID from /users endpoint (same source as dropdown)
        """
        try:
            employees = self.get_employees()
            for employee in employees:
                if employee.get('id') == user_id:
                    return employee.get('name', 'Unknown User')
            return 'Unknown User'
        except Exception as e:
            print(f"Error getting user name: {e}")
            return 'Unknown User'

    def get_user_by_id(self, user_id: str) -> Dict:
        """Get complete user object by ID from /users endpoint."""
        try:
            employees = self.get_employees()
            for employee in employees:
                if employee.get('id') == user_id:
                    return employee
            return {}
        except Exception as e:
            print(f"Error getting user by id: {e}")
            return {}
    
    def get_user_email_by_id(self, user_id: str) -> str:
        """
        Get user email by ID from /users endpoint
        """
        try:
            employees = self.get_employees()
            for employee in employees:
                if employee.get('id') == user_id:
                    return employee.get('email', '')
            return ''
        except Exception as e:
            print(f"Error getting user email: {e}")
            return ''

    def get_analytics(
        self,
        analytics_from: str,
        analytics_to: str,
        user_ids: Optional[List[str]] = None,
        organization_ids: Optional[List[str]] = None,
        time_types: Optional[List[str]] = None,
        approval_statuses: Optional[List[str]] = None,
        recipient_emails: Optional[List[str]] = None,
        sub_organizations: bool = True
    ) -> Dict:
        """Loads analytics totals from McTime for the given filter scope."""
        url = f"{self.base_url}/analytics"
        recipient_list = recipient_emails or [os.getenv('SENDER_EMAIL', 'noreply@example.com')]

        payload = {
            "analyticsFrom": analytics_from,
            "analyticsTo": analytics_to,
            "exportType": {
                "pdf": {
                    "export": False,
                    "employeeAnalytics": False,
                    "allAnalytics": False,
                    "timeEntries": False
                },
                "csv": {
                    "export": False,
                    "csvDelimiter": ";"
                },
                "json": {
                    "export": True
                }
            },
            "recipientEmails": recipient_list,
            "emailMessage": "",
            "userIds": user_ids or [],
            "organizationIds": organization_ids or [],
            "subOrganizations": sub_organizations,
            "approvalStatuses": approval_statuses or [],
            "timeTypes": time_types or []
        }

        try:
            response = requests.post(url, headers=self.headers, json=payload)
            if response.status_code != 200:
                print(f"Error fetching analytics: {response.status_code}")
                print(f"Analytics response: {response.text}")
                return {}

            data = response.json()
            items = data.get("items", [])
            if not items:
                return {}

            first_item = items[0]
            if first_item.get("message") != "Success!":
                return {}

            return first_item.get("data", {}) or {}
        except Exception as e:
            print(f"Exception in get_analytics: {e}")
            return {}
    
    def _enhance_time_record(self, time_record: Dict) -> Dict:
        """
        Enhance time record with calculated work hours, formatted breaks, and project info
        """
        from datetime import datetime, timedelta
        
        try:
            # Parse start and end times
            start_time = datetime.fromisoformat(time_record['from'].replace('Z', '+00:00'))
            end_time = datetime.fromisoformat(time_record['to'].replace('Z', '+00:00'))
            
            # Calculate total time
            total_time = end_time - start_time
            total_hours = total_time.total_seconds() / 3600
            
            # Calculate break time
            total_break_time = timedelta()
            break_list = []
            
            # Handle None breaks (fix for September 15th issue)
            breaks = time_record.get('breaks') or []
            for break_item in breaks:
                try:
                    break_start = datetime.fromisoformat(break_item['from'].replace('Z', '+00:00'))
                    break_end = datetime.fromisoformat(break_item['to'].replace('Z', '+00:00'))
                    break_duration = break_end - break_start
                    total_break_time += break_duration
                    
                    # Format break for CSV: "09:00-09:30" (wie im Original CSV)
                    break_str = f"{break_start.strftime('%H:%M')}-{break_end.strftime('%H:%M')}"
                    break_list.append(break_str)
                except Exception as e:
                    print(f"Error processing break: {e}")
            
            # Calculate actual work hours
            break_hours = total_break_time.total_seconds() / 3600
            actual_work_hours = total_hours - break_hours
            
            # Add enhanced fields
            time_record['project'] = time_record.get('organizationName', 'Unknown Project')
            time_record['total_hours'] = round(total_hours, 2)
            time_record['break_hours'] = round(break_hours, 2)
            time_record['actual_work_hours'] = round(actual_work_hours, 2)
            
            # Pausenformat für CSV: "09:00-09:30;16:00-16:30"
            time_record['breaks_formatted'] = ';'.join(break_list) if break_list else ''
            
            # Datumsformat: "01.10.25" (wie im Original CSV)
            time_record['date_formatted'] = start_time.strftime('%d.%m.%y')
            time_record['time_formatted'] = f"{start_time.strftime('%H:%M')}-{end_time.strftime('%H:%M')}"
            
            # Zeitfelder für CSV
            time_record['time_start'] = start_time.strftime('%H:%M')
            time_record['time_end'] = end_time.strftime('%H:%M')
            
            # Summen im Format "13:00" (wie im Original CSV)
            total_hours_int = int(total_hours)
            total_minutes = int((total_hours - total_hours_int) * 60)
            time_record['total_hours_formatted'] = f"{total_hours_int:02d}:{total_minutes:02d}"
            
            actual_hours_int = int(actual_work_hours)
            actual_minutes = int((actual_work_hours - actual_hours_int) * 60)
            time_record['actual_hours_formatted'] = f"{actual_hours_int:02d}:{actual_minutes:02d}"
            
            return time_record
            
        except Exception as e:
            print(f"Error enhancing time record: {e}")
            # Add default values if calculation fails
            time_record['project'] = time_record.get('organizationName', 'Unknown Project')
            time_record['total_hours'] = 0.0
            time_record['break_hours'] = 0.0
            time_record['actual_work_hours'] = 0.0
            time_record['breaks_formatted'] = ''
            time_record['date_formatted'] = 'Unknown Date'
            time_record['time_formatted'] = 'Unknown Time'
            time_record['time_start'] = '00:00'
            time_record['time_end'] = '00:00'
            time_record['total_hours_formatted'] = '00:00'
            time_record['actual_hours_formatted'] = '00:00'
            return time_record
    
    def get_time_entries(self, employee_id: str, date_from: str, date_to: str, organization_id: Optional[str] = None) -> List[Dict]:
        """
        Get time entries for specific employee and date range
        date_from and date_to should be in format: 'YYYY-MM-DD'
        """
        url = f"{self.base_url}/times"
        
        # Convert YYYY-MM-DD to ISO format with timezone
        from_datetime = f"{date_from}T00:00:00+02:00"
        to_datetime = f"{date_to}T23:59:59+02:00"
        
        params = {
            "userIds": employee_id,  # Use userIds instead of userId
            "from": from_datetime,   # Use ISO format with timezone
            "to": to_datetime        # Use ISO format with timezone
        }
        if organization_id:
            params["organizationId"] = organization_id
            
        print(f"=== MCTIME API CALL ===")
        print(f"URL: {url}")
        print(f"Headers: {self.headers}")
        print(f"Params: {params}")
            
        try:
            response = requests.get(url, headers=self.headers, params=params)
            print(f"=== MCTIME API RESPONSE ===")
            print(f"Status Code: {response.status_code}")
            print(f"Response Text: {response.text}")
            
            if response.status_code == 200:
                data = response.json()
                print(f"Parsed JSON: {data}")
                
                # Extract time entries from the nested structure
                time_entries = data.get("items", [])
                all_times = []
                
                employee = self.get_user_by_id(employee_id)
                user_name = employee.get('name') or 'Unknown User'
                print(f"Got user name from /users endpoint: '{user_name}'")
                
                for item in time_entries:
                    if item.get('message') == 'Success' and 'data' in item:
                        item_data = item['data']
                        
                        if 'timeEntries' in item_data:
                            for time_entry in item_data['timeEntries']:
                                if 'times' in time_entry:
                                    # Extract individual time records
                                    for time_record in time_entry['times']:
                                        # Add the user name from /users API (same as dropdown)
                                        time_record['name'] = user_name
                                        time_record['id'] = employee_id
                                        time_record['first_name'] = employee.get('firstName', '')
                                        time_record['last_name'] = employee.get('lastName', '')
                                        time_record['personnel_number'] = employee.get('personnel_number', '')
                                        
                                        # Calculate work hours and format breaks
                                        time_record = self._enhance_time_record(time_record)
                                        
                                        all_times.append(time_record)
                                        
                                        print(f"Added time record with name: '{time_record.get('name', 'NO_NAME')}'")
                
                print(f"Extracted {len(all_times)} individual time records")
                if all_times:
                    print(f"First record user name: '{all_times[0].get('name', 'MISSING')}'")
                return all_times
            else:
                print(f"Error fetching time entries: {response.status_code}")
                print(f"Response: {response.text}")
                return []
        except Exception as e:
            print(f"Exception in get_time_entries: {e}")
            return []

class BackendService:
    def __init__(self, api_key: str):
        self.mctime_api = McTimeAPI(api_key)
    
    def get_form_data(self) -> Dict:
        """
        Get initial data for frontend form (organizations and employees)
        """
        organizations = self.mctime_api.get_organizations()
        employees = self.mctime_api.get_employees()
        
        return {
            "organizations": organizations,
            "employees": employees,
            "status": "success"
        }
    
    def process_form_request(self, form_data: Dict) -> Dict:
        """
        Process frontend form submission and return time data
        Expected form_data format:
        {
            "firma": "organization_id",
            "mitarbeiter": "employee_id", 
            "von": "dd.mm.yyyy",
            "bis": "dd.mm.yyyy"
        }
        """
        try:
            # Extract form data
            organization_id = form_data.get("firma")
            employee_id = form_data.get("mitarbeiter")
            date_from_str = form_data.get("von")  # dd.mm.yyyy
            date_to_str = form_data.get("bis")    # dd.mm.yyyy
            
            # Convert date format from dd.mm.yyyy to yyyy-mm-dd
            date_from = self._convert_date_format(date_from_str)
            date_to = self._convert_date_format(date_to_str)
            
            if not all([employee_id, date_from, date_to]):
                return {
                    "status": "error",
                    "message": "Missing required fields: employee, date_from, date_to"
                }
            
            # Get time entries
            time_entries = self.mctime_api.get_time_entries(
                employee_id=employee_id,
                date_from=date_from,
                date_to=date_to,
                organization_id=organization_id
            )
            
            return {
                "status": "success",
                "data": {
                    "timeEntries": time_entries,
                    "employee_id": employee_id,
                    "organization_id": organization_id,
                    "date_range": {
                        "from": date_from,
                        "to": date_to
                    }
                }
            }
            
        except Exception as e:
            return {
                "status": "error",
                "message": f"Error processing request: {str(e)}"
            }
    
    def _convert_date_format(self, date_str: str) -> str:
        """
        Convert date from dd.mm.yyyy to yyyy-mm-dd format
        """
        if not date_str:
            return ""
        try:
            # Parse dd.mm.yyyy
            date_obj = datetime.strptime(date_str, "%d.%m.%Y")
            # Return yyyy-mm-dd
            return date_obj.strftime("%Y-%m-%d")
        except ValueError:
            raise ValueError(f"Invalid date format: {date_str}. Expected dd.mm.yyyy")

# Example usage and testing
if __name__ == "__main__":
    # Initialize service - NEVER hardcode API keys!
    # Use environment variables instead
    import os
    api_key = os.getenv('MCTIME_API_KEY', '')
    if not api_key:
        print("ERROR: MCTIME_API_KEY environment variable not set!")
        print("Please set it in your .env file or export it:")
        print("  export MCTIME_API_KEY='your-api-key-here'")
        exit(1)
    backend_service = BackendService(api_key)
    
    # Test getting form data
    print("=== Getting Form Data ===")
    form_data = backend_service.get_form_data()
    print(f"Organizations: {len(form_data.get('organizations', []))}")
    print(f"Employees: {len(form_data.get('employees', []))}")
    
    # Test processing form request
    print("\n=== Processing Form Request ===")
    test_form_data = {
        "firma": "some_org_id",  # Replace with actual org ID
        "mitarbeiter": "6b1dfeb5-9f94-4814-bac6-c1e760990669",  # Artiol Pepaj
        "von": "01.09.2025",
        "bis": "05.09.2025"
    }
    
    result = backend_service.process_form_request(test_form_data)
    print(f"Result status: {result.get('status')}")
    if result.get('status') == 'success':
        print(f"Time entries found: {len(result.get('data', {}).get('timeEntries', []))}")
        # Print first entry for debugging
        entries = result.get('data', {}).get('timeEntries', [])
        if entries:
            print(f"First entry: {entries[0]}")
    else:
        print(f"Error: {result.get('message')}")
        
    # Test direct API call with correct format
    print("\n=== Direct API Test ===")
    direct_entries = backend_service.mctime_api.get_time_entries(
        employee_id="6b1dfeb5-9f94-4814-bac6-c1e760990669",
        date_from="2025-09-01",
        date_to="2025-09-05"
    )
    print(f"Direct API call returned: {len(direct_entries)} entries")
    if direct_entries:
        print(f"Sample entry: {direct_entries[0]}")