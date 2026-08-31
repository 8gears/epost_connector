const STATUS_COLORS = {
	Running: "blue",
	Success: "green",
	Partial: "orange",
	Failed: "red",
};

// run_sync_now enqueues with timeout=1800, so a run still "Running" after that
// is not slow, it is dead.
const RUN_TIMEOUT_MINUTES = 30;

frappe.ui.form.on("ePost Sync Log", {
	refresh(frm) {
		frm.page.set_indicator(__(frm.doc.status), STATUS_COLORS[frm.doc.status] || "gray");

		set_headline(frm);

		if (frm.doc.errors) {
			frm.add_custom_button(__("Letters with Sync Errors"), () => {
				frappe.route_options = { sync_error: ["is", "set"] };
				frappe.set_route("List", "ePost Letter");
			});
		}

		frm.add_custom_button(__("ePost Settings"), () => frappe.set_route("Form", "ePost Settings"));
	},
});

function set_headline(frm) {
	frm.dashboard.clear_headline();

	if (frm.doc.status === "Running" && is_abandoned(frm.doc.started_at)) {
		frm.dashboard.set_headline(
			__(
				"This run has been Running for over {0} minutes. The worker died before it could finish; the letters it had already written are still there.",
				[RUN_TIMEOUT_MINUTES]
			),
			"red",
			true
		);
		return;
	}

	if (frm.doc.status === "Partial") {
		frm.dashboard.set_headline(
			__("Some letters failed. The rest of the run completed; see Errors below."),
			"orange",
			true
		);
	}
}

function is_abandoned(started_at) {
	if (!started_at) {
		return false;
	}
	// started_at is stored in the system timezone, so compare it against that.
	return moment(started_at).isBefore(
		moment(frappe.datetime.system_datetime()).subtract(RUN_TIMEOUT_MINUTES, "minutes")
	);
}
