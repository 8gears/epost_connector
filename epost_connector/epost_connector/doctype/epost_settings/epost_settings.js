const SYNC_LOG_COLORS = {
	Running: "blue",
	Success: "green",
	Partial: "orange",
	Failed: "red",
};

// How long the "Sync Now" button waits for the background job to open its log
// before it stops watching. A queued job with no worker never opens one.
const LOG_POLL_ATTEMPTS = 10;
const LOG_POLL_INTERVAL_MS = 2000;

frappe.ui.form.on("ePost Settings", {
	refresh(frm) {
		frm.add_custom_button(__("Test Connection"), () => test_connection(frm), __("ePost"));
		frm.add_custom_button(__("Fetch Tenants"), () => start_fetch_tenants(frm), __("ePost"));
		frm.add_custom_button(__("Sync Log"), () => frappe.set_route("List", "ePost Sync Log"));
		frm.add_custom_button(__("Sync Now"), () => sync_now(frm)).addClass("btn-primary");

		["default_expense_account", "default_cost_center"].forEach((field) => {
			frm.set_query(field, () => ({ filters: { company: frm.doc.company, is_group: 0 } }));
		});

		show_connection_state(frm);
	},

	// The "Fetch Tenants" Button field on the form.
	fetch_tenants(frm) {
		start_fetch_tenants(frm);
	},
});

function show_connection_state(frm) {
	frm.dashboard.clear_headline();

	if (!frm.doc.username || !frm.doc.tenant_id) {
		frm.dashboard.set_headline(
			__("Fill in the credentials, then press Fetch Tenants to pick a tenant."),
			"orange",
			true
		);
		return;
	}

	latest_sync_log().then((log) => {
		const lines = [];

		lines.push(
			frm.doc.enabled
				? __("The hourly sync is on.")
				: __("The hourly sync is off. Sync Now still runs.")
		);

		if (log) {
			lines.push(
				__("Last run {0}: {1}, {2}", [
					`<a href="/app/epost-sync-log/${encodeURIComponent(log.name)}">${frappe.datetime.comment_when(
						log.started_at || log.creation
					)}</a>`,
					__(log.status),
					__("{0} seen, {1} new, {2} downloaded", [
						log.letters_seen || 0,
						log.letters_new || 0,
						log.files_downloaded || 0,
					]),
				])
			);
		} else {
			lines.push(__("No sync has run yet."));
		}

		frm.dashboard.set_headline(
			lines.join("<br>"),
			log ? SYNC_LOG_COLORS[log.status] || "blue" : "blue",
			true
		);
	});
}

function latest_sync_log(after) {
	const filters = after ? [["creation", ">", after]] : [];
	return frappe.db
		.get_list("ePost Sync Log", {
			filters,
			fields: [
				"name",
				"status",
				"started_at",
				"creation",
				"letters_seen",
				"letters_new",
				"files_downloaded",
			],
			order_by: "creation desc",
			limit: 1,
		})
		.then((rows) => (rows && rows.length ? rows[0] : null));
}

function sync_now(frm) {
	// creation is written in the system timezone, so compare against that.
	const queued_at = frappe.datetime.system_datetime();

	frappe.call({
		method: "epost_connector.epost.sync.run_sync_now",
		freeze: true,
		freeze_message: __("Queueing the sync..."),
		callback: () => watch_for_log(frm, queued_at, LOG_POLL_ATTEMPTS),
	});
}

function watch_for_log(frm, queued_at, attempts_left) {
	latest_sync_log(queued_at).then((log) => {
		if (log) {
			frappe.show_alert({
				message: __("Sync started: {0}", [
					`<a href="/app/epost-sync-log/${encodeURIComponent(log.name)}">${frappe.utils.escape_html(
						log.name
					)}</a>`,
				]),
				indicator: "blue",
			});
			// The log row appears when the run starts, so Last Sync At still holds
			// the previous run. Show the live log rather than reload stale fields.
			show_connection_state(frm);
			return;
		}

		if (attempts_left <= 1) {
			frappe.show_alert({
				message: __("Sync is queued but has not started. Check that a background worker is running."),
				indicator: "orange",
			});
			return;
		}

		setTimeout(() => watch_for_log(frm, queued_at, attempts_left - 1), LOG_POLL_INTERVAL_MS);
	});
}

function start_fetch_tenants(frm) {
	// The endpoint reads the stored password, so unsaved edits must land first.
	// frm.save() on a clean document never resolves, so it is only called when
	// there is something to save.
	const saved = frm.is_dirty() ? frm.save() : Promise.resolve();
	saved.then(() => request_tenants(frm));
}

function request_tenants(frm) {
	frappe.call({
		method: "epost_connector.epost_connector.doctype.epost_settings.epost_settings.fetch_tenants",
		freeze: true,
		freeze_message: __("Asking ePost for tenants..."),
		callback: ({ message: tenants }) => {
			if (!tenants || !tenants.length) {
				frappe.msgprint(__("No tenants returned for these credentials."));
				return;
			}
			if (tenants.length === 1) {
				apply_tenant(frm, tenants[0]);
				return;
			}
			pick_tenant(frm, tenants);
		},
	});
}

function pick_tenant(frm, tenants) {
	const dialog = new frappe.ui.Dialog({
		title: __("Select Tenant"),
		fields: [
			{
				fieldname: "tenant",
				fieldtype: "Select",
				label: __("Tenant"),
				reqd: 1,
				default: "0",
				options: tenants.map((t, index) => ({
					value: String(index),
					label: `${t.company_name || t.tenant_id} (company ${t.company_id})`,
				})),
			},
			{
				fieldname: "note",
				fieldtype: "HTML",
				options: `<p class="text-muted small">${__(
					"A token is only ever valid for one tenant and company pair, so this choice decides which letterbox is synced."
				)}</p>`,
			},
		],
		primary_action_label: __("Use This Tenant"),
		primary_action: ({ tenant }) => {
			dialog.hide();
			apply_tenant(frm, tenants[Number(tenant)]);
		},
	});
	dialog.show();
}

function apply_tenant(frm, tenant) {
	frm.set_value("tenant_id", tenant.tenant_id);
	frm.set_value("company_id", String(tenant.company_id));

	const announce = () =>
		frappe.show_alert({
			message: __("Tenant {0} selected.", [frappe.utils.escape_html(tenant.tenant_id)]),
			indicator: "green",
		});

	// Re-picking the tenant already stored leaves the document clean, and a
	// clean frm.save() never resolves.
	frm.is_dirty() ? frm.save().then(announce) : announce();
}

function test_connection(frm) {
	frappe.call({
		method: "epost_connector.epost_connector.doctype.epost_settings.epost_settings.test_connection",
		freeze: true,
		freeze_message: __("Talking to ePost..."),
		callback: ({ message }) => {
			if (!message) return;
			frappe.msgprint({
				title: __("Connection OK"),
				indicator: "green",
				message: __("Tenant {0}, {1} unread letters in the inbox.", [
					message.tenant_id,
					message.unread_letters,
				]),
			});
		},
	});
}
