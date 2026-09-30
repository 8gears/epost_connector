// Matches TERMINAL_STATUSES in epost/sync.py: the pipeline never leaves these.
const TERMINAL_STATUSES = ["Drafted", "Not Bookable", "Duplicate", "Ignored"];

frappe.ui.form.on("ePost Letter", {
	refresh(frm) {
		// Every transition but "Ignored" is a server decision, and "Ignored" has
		// its own button, so the field itself is never edited by hand.
		frm.set_df_property("status", "read_only", 1);
		// The supplier is the reviewer's answer only while the letter waits for it.
		frm.set_df_property("supplier", "read_only", frm.doc.status !== "Waiting for Supplier");

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
	const esc = frappe.utils.escape_html;

	if (frm.doc.sync_error) {
		frm.dashboard.set_headline(
			`<b>${__("The last sync could not process this letter")}</b><br>${esc(frm.doc.sync_error)}`,
			"red",
			true
		);
		return;
	}

	if (frm.doc.status === "Drafted" && frm.doc.purchase_invoice) {
		frm.dashboard.set_headline(
			// get_form_link builds the current Desk route; a hardcoded /app/... only
			// reaches it through a redirect, which reloads out of the SPA.
			__("Drafted as {0}. Review and submit it there.", [
				frappe.utils.get_form_link("Purchase Invoice", frm.doc.purchase_invoice, true),
			]),
			"green",
			true
		);
		return;
	}

	if (frm.doc.processing_note) {
		const color = frm.doc.status === "Waiting for Supplier" ? "orange" : "blue";
		frm.dashboard.set_headline(esc(frm.doc.processing_note), color, true);
	}
}

function add_actions(frm) {
	if (frm.is_new()) {
		return;
	}

	const terminal = TERMINAL_STATUSES.includes(frm.doc.status);

	if (frm.doc.file && frm.doc.status !== "Drafted") {
		frm.add_custom_button(__("Process again"), () => process(frm, 1), __("Actions"));
	}
	if (!terminal) {
		frm.add_custom_button(__("Mark as Not Bookable"), () => not_bookable(frm), __("Actions"));
		frm.add_custom_button(__("Mark as Ignored"), () => ignore(frm), __("Actions"));
	}

	if (frm.doc.purchase_invoice) {
		frm.add_custom_button(__("Open Purchase Invoice"), () =>
			frappe.set_route("Form", "Purchase Invoice", frm.doc.purchase_invoice)
		).addClass("btn-primary");
		return;
	}

	// One primary action at a time, and it is whatever moves this letter forward.
	if (!frm.doc.file) {
		frm.add_custom_button(__("Download PDF"), () => download_pdf(frm)).addClass("btn-primary");
	} else if (frm.doc.status === "Waiting for Supplier") {
		frm.add_custom_button(__("Map Supplier"), () => map_supplier(frm)).addClass("btn-primary");
		frm.add_custom_button(__("Create Supplier"), () => create_supplier(frm));
	} else if (frm.doc.status === "Downloaded") {
		frm.add_custom_button(__("Process"), () => process(frm, 0)).addClass("btn-primary");
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
	frappe.db.get_value("File", { file_url: frm.doc.file }, "name").then(
		(r) => {
			if (!r || !r.message || !r.message.name) {
				render_empty_preview(frm, wrapper, __("The PDF record for this letter no longer exists."));
				return;
			}
			render_iframe(frm, wrapper);
		},
		// A probe that could not run is not evidence the file is gone, so show
		// the preview and let the iframe speak for itself.
		() => render_iframe(frm, wrapper)
	);
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

function process(frm, force) {
	frappe.call({
		method: "epost_connector.inbox.process.process",
		args: { letter_name: frm.doc.name, force },
		freeze: true,
		freeze_message: __("Reading the letter and matching the supplier..."),
		callback: () => frm.reload_doc(),
	});
}

function not_bookable(frm) {
	frm.set_value("status", "Not Bookable").then(() => frm.save());
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

function supplier_hints(frm) {
	return frappe.xcall("epost_connector.inbox.process.supplier_hints", { letter_name: frm.doc.name });
}

function issuer_html(hints) {
	const esc = frappe.utils.escape_html;
	const rows = [
		[__("Name"), hints.vendor_name],
		[__("VAT / UID"), hints.vendor_tax_id],
		[__("Country"), hints.vendor_country],
		[__("Address"), hints.vendor_address],
		[__("IBAN"), hints.iban],
	]
		.filter(([, value]) => value)
		.map(([label, value]) => `<tr><td class="text-muted">${label}</td><td>${esc(value).replace(/\n/g, "<br>")}</td></tr>`)
		.join("");
	return `<p class="text-muted small">${__("Read off the letter")}</p><table class="table table-sm small">${rows}</table>`;
}

function map_supplier(frm) {
	supplier_hints(frm).then((hints) => {
		const guesses = (hints.candidates || []).map((c) => c.supplier);
		const dialog = new frappe.ui.Dialog({
			title: __("Map Supplier"),
			fields: [
				{ fieldname: "issuer", fieldtype: "HTML", options: issuer_html(hints) },
				{
					fieldname: "supplier",
					fieldtype: "Link",
					label: __("Supplier"),
					options: "Supplier",
					reqd: 1,
					default: guesses[0],
					description: guesses.length
						? __("Closest existing: {0}", [guesses.map(frappe.utils.escape_html).join(", ")])
						: __("No existing supplier looks similar. Create one instead."),
				},
			],
			primary_action_label: __("Map and continue"),
			primary_action: ({ supplier }) => {
				dialog.hide();
				frm.set_value("supplier", supplier).then(() => frm.save());
			},
		});
		dialog.show();
	});
}

function create_supplier(frm) {
	supplier_hints(frm).then((hints) => {
		const dialog = new frappe.ui.Dialog({
			title: __("Create Supplier"),
			fields: [
				{ fieldname: "issuer", fieldtype: "HTML", options: issuer_html(hints) },
				{
					fieldname: "supplier_name",
					fieldtype: "Data",
					label: __("Supplier Name"),
					reqd: 1,
					default: hints.vendor_name,
				},
				{ fieldname: "tax_id", fieldtype: "Data", label: __("Tax ID"), default: hints.vendor_tax_id },
				{
					fieldname: "country",
					fieldtype: "Link",
					label: __("Country"),
					options: "Country",
					default: hints.vendor_country,
				},
			],
			primary_action_label: __("Create and continue"),
			primary_action: (values) => {
				dialog.hide();
				frappe.call({
					method: "epost_connector.inbox.process.create_supplier",
					args: { letter_name: frm.doc.name, ...values },
					freeze: true,
					freeze_message: __("Creating the supplier..."),
					callback: () => frm.reload_doc(),
				});
			},
		});
		dialog.show();
	});
}
