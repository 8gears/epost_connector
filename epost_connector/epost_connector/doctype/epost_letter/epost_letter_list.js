const STATUS_COLORS = {
	New: "orange",
	Downloaded: "blue",
	Analyzed: "purple",
	Imported: "green",
	Ignored: "gray",
};

// The views a bookkeeper actually works from, offered under a Quick Filters
// button. "To Import" is the queue; "Sync Errors" is what needs a human.
const QUICK_FILTERS = {
	"New Letters": [["status", "=", "New"]],
	"To Import": [["status", "in", ["Downloaded", "Analyzed"]]],
	"Sync Errors": [["sync_error", "is", "set"]],
	Imported: [["status", "=", "Imported"]],
};

frappe.listview_settings["ePost Letter"] = {
	add_fields: ["status", "file", "purchase_invoice", "sync_error"],

	// Ignored letters are a decision already taken; they are one click away
	// under Quick Filters rather than in everyone's default view.
	filters: [["status", "!=", "Ignored"]],

	get_indicator(doc) {
		if (doc.sync_error) {
			return [__("Sync Error"), "red", "sync_error,is,set"];
		}
		return [__(doc.status), STATUS_COLORS[doc.status] || "gray", `status,=,${doc.status}`];
	},

	formatters: {
		received_at(value) {
			return value ? frappe.datetime.comment_when(value) : "";
		},
	},

	// A bookkeeper scans the list and wants the scan itself, not the record.
	button: {
		show: (doc) => Boolean(doc.file),
		get_label: () => __("PDF"),
		get_description: (doc) => __("Open the PDF for {0}", [doc.title || doc.name]),
		action: (doc) => window.open(doc.file, "_blank", "noopener"),
	},

	onload(listview) {
		// Mirrors frappe.only_for() on run_sync_now, so an Accounts User is not
		// shown a button that can only answer with a permission error.
		if (can_sync()) {
			listview.page.add_inner_button(__("Sync Now"), () => sync_now(listview));
		}

		Object.keys(QUICK_FILTERS).forEach((label) => {
			listview.page.add_inner_button(
				__(label),
				() => apply_quick_filter(listview, QUICK_FILTERS[label]),
				__("Quick Filters")
			);
		});
		listview.page.add_inner_button(
			__("Everything"),
			() => apply_quick_filter(listview, []),
			__("Quick Filters")
		);

		listview.page.add_action_item(__("Mark as Ignored"), () => bulk_ignore(listview));
	},
};

function can_sync() {
	return ["System Manager", "Accounts Manager"].some((role) => frappe.user_roles.includes(role));
}

function apply_quick_filter(listview, filters) {
	// clear() refreshes on its own, so "Everything" needs nothing after it.
	listview.filter_area
		.clear()
		.then(() => listview.filter_area.add(filters.map((f) => ["ePost Letter", ...f])));
}

function bulk_ignore(listview) {
	const names = listview.get_checked_items(true);
	if (!names.length) {
		return;
	}

	frappe.confirm(
		__("Ignore {0} letters? They stay in ERPNext but drop out of the import queue.", [names.length]),
		() => {
			frappe
				.xcall("frappe.desk.doctype.bulk_update.bulk_update.submit_cancel_or_update_docs", {
					doctype: "ePost Letter",
					docnames: names,
					action: "update",
					data: { status: "Ignored" },
				})
				.then(() => listview.refresh());
		}
	);
}

function sync_now(listview) {
	frappe.call({
		method: "epost_connector.epost.sync.run_sync_now",
		freeze: true,
		freeze_message: __("Queueing the sync..."),
		callback: () => {
			frappe.show_alert({
				message: __("Sync queued. Every run writes an {0}.", [
					`<a href="/app/epost-sync-log">${__("ePost Sync Log")}</a>`,
				]),
				indicator: "blue",
			});
			listview.refresh();
		},
	});
}
