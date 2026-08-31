app_name = "epost_connector"
app_title = "ePost Connector"
app_publisher = "8gears"
app_description = "Sync the Swiss Post ePost / KLARA digital letterbox into ERPNext"
app_email = "vadim@8gears.com"
app_license = "mit"

# Purchase Invoice creation and the Company/expense-account defaults come from ERPNext.
required_apps = ["erpnext"]

scheduler_events = {
	"hourly_long": [
		"epost_connector.epost.sync.scheduled_sync",
	],
}

# The letterbox is small and the run is I/O bound; "long" keeps a slow ePost
# response from blocking the shared default queue.
