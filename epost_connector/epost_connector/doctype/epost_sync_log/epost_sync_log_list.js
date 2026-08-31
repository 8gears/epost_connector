frappe.listview_settings["ePost Sync Log"] = {
	add_fields: ["status", "errors", "letters_seen", "letters_new"],

	get_indicator(doc) {
		const colors = {
			Running: "blue",
			Success: "green",
			Partial: "orange",
			Failed: "red",
		};
		return [__(doc.status), colors[doc.status] || "gray", `status,=,${doc.status}`];
	},

	formatters: {
		started_at(value) {
			return value ? frappe.datetime.comment_when(value) : "";
		},
	},
};
