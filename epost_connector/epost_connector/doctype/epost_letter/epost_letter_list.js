frappe.listview_settings["ePost Letter"] = {
	add_fields: ["status", "file", "purchase_invoice", "sync_error"],
	filters: [["status", "!=", "Ignored"]],

	get_indicator(doc) {
		const colors = {
			New: "orange",
			Downloaded: "blue",
			Analyzed: "purple",
			Imported: "green",
			Ignored: "gray",
		};
		if (doc.sync_error) {
			return [__("Sync Error"), "red", "sync_error,is,set"];
		}
		return [__(doc.status), colors[doc.status] || "gray", `status,=,${doc.status}`];
	},

	onload(listview) {
		listview.page.add_inner_button(__("Sync Now"), () => {
			frappe.call({
				method: "epost_connector.epost.sync.run_sync_now",
				freeze: true,
				callback: () => {
					frappe.show_alert({
						message: __("Sync queued. Check ePost Sync Log."),
						indicator: "blue",
					});
				},
			});
		});
	},
};
