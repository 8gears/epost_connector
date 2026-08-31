frappe.ui.form.on("ePost Settings", {
	refresh(frm) {
		frm.add_custom_button(__("Test Connection"), () => test_connection(frm));
		frm.add_custom_button(__("Sync Now"), () => sync_now(frm));

		["default_expense_account", "default_cost_center"].forEach((field) => {
			frm.set_query(field, () => ({ filters: { company: frm.doc.company, is_group: 0 } }));
		});
	},

	fetch_tenants(frm) {
		// The endpoint reads the stored password, so unsaved edits must land first.
		frm.save().then(() => fetch_tenants(frm));
	},
});

function fetch_tenants(frm) {
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
				options: tenants.map((t, index) => ({
					value: String(index),
					label: `${t.company_name || t.tenant_id} (company ${t.company_id})`,
				})),
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
	frm.save();
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

function sync_now(frm) {
	frappe.call({
		method: "epost_connector.epost.sync.run_sync_now",
		freeze: true,
		callback: () => {
			frappe.show_alert({ message: __("Sync queued. Check ePost Sync Log."), indicator: "blue" });
		},
	});
}
