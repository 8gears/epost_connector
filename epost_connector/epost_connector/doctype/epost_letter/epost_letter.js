const STATUS_COLORS = {
	New: "orange",
	Downloaded: "blue",
	Analyzed: "purple",
	Imported: "green",
	Ignored: "gray",
};

frappe.ui.form.on("ePost Letter", {
	refresh(frm) {
		render_preview(frm);

		if (frm.doc.status) {
			frm.page.set_indicator(__(frm.doc.status), STATUS_COLORS[frm.doc.status] || "gray");
		}

		if (!frm.doc.file) {
			frm.add_custom_button(__("Download PDF"), () => download_pdf(frm));
		} else {
			frm.add_custom_button(__("Open PDF"), () => window.open(frm.doc.file, "_blank"));
			frm.add_custom_button(__("Analyze"), () => analyze(frm));
		}

		if (!frm.doc.purchase_invoice) {
			frm.add_custom_button(__("Create Purchase Invoice"), () => create_purchase_invoice(frm)).addClass(
				"btn-primary"
			);
		}
	},
});

function render_preview(frm) {
	const wrapper = frm.get_field("pdf_preview").$wrapper;
	wrapper.empty();

	if (!frm.doc.file) {
		wrapper.html(`<div class="text-muted">${__("No PDF downloaded yet.")}</div>`);
		return;
	}

	// The file is private; the iframe rides the user's own Desk session, so a
	// user without read access on this letter sees nothing.
	wrapper.html(`
		<iframe
			src="${frappe.utils.escape_html(frm.doc.file)}#view=FitH"
			title="${__("Letter PDF")}"
			style="width:100%;height:70vh;border:1px solid var(--border-color);border-radius:var(--border-radius-md);background:var(--card-bg);"
		></iframe>
	`);
}

function download_pdf(frm) {
	frappe.call({
		method: "epost_connector.epost_connector.doctype.epost_letter.epost_letter.download_pdf",
		args: { letter_name: frm.doc.name },
		freeze: true,
		freeze_message: __("Fetching the PDF from ePost..."),
		callback: () => frm.reload_doc(),
	});
}

function analyze(frm) {
	frappe.call({
		method: "epost_connector.extraction.pipeline.analyze",
		args: { letter_name: frm.doc.name },
		freeze: true,
		freeze_message: __("Running the extractor..."),
		callback: ({ message }) => {
			frm.reload_doc();
			if (message && !message.extracted) {
				frappe.show_alert({
					message: __("The configured extractor returned nothing."),
					indicator: "orange",
				});
			}
		},
	});
}

function create_purchase_invoice(frm) {
	frappe.call({
		method: "epost_connector.epost.import_invoice.suggest_supplier",
		args: { letter_name: frm.doc.name },
		callback: ({ message }) => open_supplier_dialog(frm, (message && message.supplier) || null),
	});
}

function open_supplier_dialog(frm, suggested) {
	const dialog = new frappe.ui.Dialog({
		title: __("Create Purchase Invoice"),
		fields: [
			{
				fieldname: "supplier",
				fieldtype: "Link",
				label: __("Supplier"),
				options: "Supplier",
				reqd: 1,
				default: suggested,
				description: suggested
					? __("Matched from the letter. Change it if this is wrong.")
					: __("No supplier matched this letter, pick one."),
			},
			{
				fieldname: "note",
				fieldtype: "HTML",
				options: `<p class="text-muted small">${__(
					"The invoice is created as a draft and is never submitted. Amounts and accounts are yours to complete."
				)}</p>`,
			},
		],
		primary_action_label: __("Create Draft"),
		primary_action: ({ supplier }) => {
			dialog.hide();
			frappe.call({
				method: "epost_connector.epost.import_invoice.create_purchase_invoice",
				args: { letter_name: frm.doc.name, supplier },
				freeze: true,
				callback: ({ message }) => {
					if (!message) return;
					frm.reload_doc();
					frappe.set_route("Form", "Purchase Invoice", message.purchase_invoice);
				},
			});
		},
	});
	dialog.show();
}
