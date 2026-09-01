// Matches TERMINAL_STATUSES in epost/sync.py: the pipeline never leaves these.
const TERMINAL_STATUSES = ["Imported", "Ignored"];

frappe.ui.form.on("ePost Letter", {
	refresh(frm) {
		// Every transition but "Ignored" is a server decision, and "Ignored" has
		// its own button, so the field itself is never edited by hand.
		frm.set_df_property("status", "read_only", 1);

		// The page indicator is not set here on purpose. getdoctype ships
		// epost_letter_list.js as meta.__list_js and model.js:255 evaluates it on
		// the form route too, so frappe.get_indicator (indicator.js:87) already
		// reaches listview_settings.get_indicator. Setting it again here would
		// also clobber frappe's own "Not Saved" indicator on a dirty form.
		set_headline(frm);
		render_preview(frm);
		add_actions(frm);
	},
});

function set_headline(frm) {
	frm.dashboard.clear_headline();

	if (frm.doc.sync_error) {
		frm.dashboard.set_headline(
			`<b>${__("The last sync could not process this letter")}</b><br>
			${frappe.utils.escape_html(frm.doc.sync_error)}`,
			"red",
			true
		);
		return;
	}

	if (frm.doc.status === "Imported" && frm.doc.purchase_invoice) {
		frm.dashboard.set_headline(
			// get_form_link builds the current Desk route; a hardcoded /app/... only
			// reaches it through a redirect, which reloads out of the SPA.
			__("Imported as {0}.", [
				frappe.utils.get_form_link("Purchase Invoice", frm.doc.purchase_invoice, true),
			]),
			"green",
			true
		);
	}
}

function add_actions(frm) {
	if (frm.is_new()) {
		return;
	}

	const terminal = TERMINAL_STATUSES.includes(frm.doc.status);

	if (frm.doc.file) {
		frm.add_custom_button(__("Analyze"), () => analyze(frm), __("Actions"));
	}
	if (!terminal) {
		frm.add_custom_button(__("Mark as Ignored"), () => ignore(frm), __("Actions"));
	}

	if (frm.doc.purchase_invoice) {
		frm.add_custom_button(__("Open Purchase Invoice"), () =>
			frappe.set_route("Form", "Purchase Invoice", frm.doc.purchase_invoice)
		);
		return;
	}

	// One primary action at a time, and it is whatever moves this letter forward.
	if (!frm.doc.file) {
		frm.add_custom_button(__("Download PDF"), () => download_pdf(frm)).addClass("btn-primary");
	} else if (frm.doc.status !== "Ignored") {
		frm.add_custom_button(__("Create Purchase Invoice"), () => create_purchase_invoice(frm)).addClass(
			"btn-primary"
		);
	}
}

function render_preview(frm) {
	const wrapper = frm.get_field("pdf_preview").$wrapper.empty();

	if (!frm.doc.file) {
		render_empty_preview(frm, wrapper);
		return;
	}

	// `file` is a plain URL string, so it can outlive the File row it points at.
	// Without this the iframe would 403 and render as a blank rectangle with no
	// explanation. A user who can open this form can read its attachment, so a
	// missing row means the file is gone, not that access was refused.
	frappe.db.get_value("File", { file_url: frm.doc.file }, "name").then(({ message }) => {
		if (!message || !message.name) {
			render_empty_preview(frm, wrapper, __("The PDF record for this letter no longer exists."));
			return;
		}
		render_iframe(frm, wrapper);
	});
}

function render_iframe(frm, wrapper) {
	// The file is private; the iframe rides the user's own Desk session.
	// .pdf is not in frappe's FORCE_DOWNLOAD_EXTENSIONS (utils/response.py:309),
	// so it is served inline as application/pdf rather than as a download.
	const url = frappe.utils.escape_html(frm.doc.file);
	$(`
		<div class="flex justify-between align-center mb-2">
			<span class="text-muted small ellipsis">${frappe.utils.escape_html(
				frm.doc.file.split("/").pop()
			)}</span>
			<a class="btn btn-default btn-xs" href="${url}" target="_blank" rel="noopener">
				${__("Open in new tab")}
			</a>
		</div>
		<iframe
			src="${url}#view=FitH"
			title="${__("Letter PDF")}"
			style="width:100%;height:70vh;border:1px solid var(--border-color);border-radius:var(--border-radius-md);background:var(--card-bg);"
		></iframe>
	`).appendTo(wrapper);
}

function render_empty_preview(frm, wrapper, reason) {
	const message =
		reason ||
		(frm.doc.sync_error
			? __("The PDF could not be fetched. See the error above.")
			: __("No PDF has been downloaded for this letter yet."));

	const $empty = $(`
		<div class="text-center text-muted py-5">
			<div>${message}</div>
		</div>
	`).appendTo(wrapper);

	if (!frm.is_new()) {
		$(`<button class="btn btn-default btn-sm mt-3">${__("Download PDF")}</button>`)
			.on("click", () => download_pdf(frm))
			.appendTo($empty);
	}
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

function ignore(frm) {
	frappe.confirm(
		__("Ignore letter {0}? It stays in ERPNext but drops out of the import queue.", [
			frappe.utils.escape_html(frm.doc.title || frm.doc.letter_id),
		]),
		// set_value resolves after the change handlers run, so save only then.
		() => frm.set_value("status", "Ignored").then(() => frm.save())
	);
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
				freeze_message: __("Creating the draft invoice..."),
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
