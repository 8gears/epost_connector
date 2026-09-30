// Matches TERMINAL_STATUSES in epost/sync.py: the pipeline never leaves these.
const TERMINAL_STATUSES = ["Drafted", "Not Bookable", "Duplicate", "Ignored"];

frappe.ui.form.on("ePost Letter", {
	refresh(frm) {
		// Every transition but "Ignored" is a server decision, and "Ignored" has
		// its own button, so the field itself is never edited by hand.
		frm.set_df_property("status", "read_only", 1);
		// Supplier and type change through the Review dialog, which also replaces
		// the draft and teaches the matcher; a bare field edit would do neither.
		frm.set_df_property("supplier", "read_only", 1);

		// The page indicator is not set here on purpose. getdoctype ships
		// epost_letter_list.js as meta.__list_js and model.js:255 evaluates it on
		// the form route too, so frappe.get_indicator (indicator.js:87) already
		// reaches listview_settings.get_indicator. Setting it again here would
		// also clobber frappe's own "Not Saved" indicator on a dirty form.
		set_headline(frm);
		render_preview(frm);
		add_actions(frm);

		// The Purchase Invoice form sends people here to review its letter.
		if (frappe.route_options && frappe.route_options.epost_review) {
			frappe.route_options = null;
			review(frm);
		}
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
		const color = ["Waiting for Supplier", "Needs Review"].includes(frm.doc.status) ? "orange" : "blue";
		frm.dashboard.set_headline(esc(frm.doc.processing_note), color, true);
	}
}

function add_actions(frm) {
	if (frm.is_new()) {
		return;
	}

	const terminal = TERMINAL_STATUSES.includes(frm.doc.status);
	// Anything the pipeline decided can be reviewed, until the invoice is submitted.
	const reviewable = frm.doc.extraction_log && !["New", "Downloaded", "Ignored"].includes(frm.doc.status);

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
		if (reviewable) {
			frm.add_custom_button(__("Review Supplier and Type"), () => review(frm));
		}
		return;
	}

	// One primary action at a time, and it is whatever moves this letter forward.
	if (!frm.doc.file) {
		frm.add_custom_button(__("Download PDF"), () => download_pdf(frm)).addClass("btn-primary");
	} else if (frm.doc.status === "Downloaded") {
		frm.add_custom_button(__("Process"), () => process(frm, 0)).addClass("btn-primary");
	} else if (reviewable) {
		const button = frm.add_custom_button(__("Review"), () => review(frm));
		if (["Waiting for Supplier", "Needs Review", "Duplicate"].includes(frm.doc.status)) {
			button.addClass("btn-primary");
		}
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
	frappe
		.xcall("epost_connector.inbox.process.mark_not_bookable", { names: [frm.doc.name] })
		.then(() => frm.reload_doc());
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

function issuer_html(hints) {
	const esc = frappe.utils.escape_html;
	const rows = [
		[__("Name"), hints.vendor_name],
		[__("VAT / UID"), hints.vendor_tax_id],
		[__("Country"), hints.vendor_country],
		[__("Address"), hints.vendor_address],
		[__("IBAN"), hints.iban],
		[__("Invoice No."), hints.invoice_number],
		[__("Type as read"), hints.read_kind && __(hints.read_kind)],
		[__("Supplier found"), hints.supplier_match],
		[__("Note"), hints.note],
	]
		.filter(([, value]) => value)
		.map(([label, value]) => `<tr><td class="text-muted">${label}</td><td>${esc(value).replace(/\n/g, "<br>")}</td></tr>`)
		.join("");
	return `<p class="text-muted small">${__("Read off the letter")}</p><table class="table table-sm small">${rows}</table>`;
}

// Matches NOT_BOOKABLE_KINDS in inbox/process.py.
const NOT_BOOKABLE_KINDS = ["Reminder", "Contract", "Correspondence", "Other"];

function review(frm) {
	frappe.xcall("epost_connector.inbox.process.review_hints", { letter_name: frm.doc.name }).then((hints) => {
		if (hints.submitted_invoice) {
			frappe.msgprint(
				__("Purchase Invoice {0} is submitted. Cancel it first, then review the letter.", [
					frappe.utils.get_form_link("Purchase Invoice", hints.submitted_invoice, true),
				])
			);
			return;
		}
		const guesses = (hints.candidates || []).map((c) => c.supplier);
		const kinds = frappe.meta.get_docfield("ePost Letter", "document_kind").options;
		// Declared first: setting the defaults fires onchange before `new` returns.
		let dialog = null;
		dialog = new frappe.ui.Dialog({
			title: __("Review Supplier and Type"),
			size: "large",
			fields: [
				{ fieldname: "issuer", fieldtype: "HTML", options: issuer_html(hints) },
				{
					fieldname: "document_kind",
					fieldtype: "Select",
					label: __("Document Type"),
					options: kinds,
					reqd: 1,
					default: hints.document_kind,
					onchange: () => set_primary_label(dialog),
				},
				{ fieldname: "supplier_section", fieldtype: "Section Break", label: __("Supplier") },
				{
					fieldname: "supplier",
					fieldtype: "Link",
					label: __("Existing Supplier"),
					options: "Supplier",
					default: hints.supplier || guesses[0],
					depends_on: "eval:!doc.create_new",
					description: guesses.length
						? __("Closest existing: {0}", [guesses.map(frappe.utils.escape_html).join(", ")])
						: __("No existing supplier looks similar."),
				},
				{ fieldname: "create_new", fieldtype: "Check", label: __("Create a new supplier instead") },
				{
					fieldname: "new_supplier_name",
					fieldtype: "Data",
					label: __("Supplier Name"),
					default: hints.vendor_name,
					depends_on: "create_new",
					mandatory_depends_on: "create_new",
				},
				{
					fieldname: "tax_id",
					fieldtype: "Data",
					label: __("Tax ID"),
					default: hints.vendor_tax_id,
					depends_on: "create_new",
				},
				{
					fieldname: "country",
					fieldtype: "Link",
					label: __("Country"),
					options: "Country",
					default: hints.vendor_country,
					depends_on: "create_new",
				},
				{
					fieldname: "not_duplicate",
					fieldtype: "Check",
					label: __("Not a duplicate: the invoice number was misread"),
					hidden: frm.doc.status !== "Duplicate",
				},
			],
			primary_action: (values) => {
				const bookable = !NOT_BOOKABLE_KINDS.includes(values.document_kind);
				if (bookable && !values.create_new && !values.supplier) {
					frappe.msgprint(__("Choose a supplier or create a new one."));
					return;
				}
				dialog.hide();
				frappe.call({
					method: "epost_connector.inbox.process.review",
					args: {
						letter_name: frm.doc.name,
						document_kind: values.document_kind,
						supplier: values.create_new ? null : values.supplier,
						new_supplier_name: values.create_new ? values.new_supplier_name : null,
						tax_id: values.create_new ? values.tax_id : null,
						country: values.create_new ? values.country : null,
						not_duplicate: values.not_duplicate ? 1 : 0,
					},
					freeze: true,
					freeze_message: __("Building the draft again..."),
					callback: () => frm.reload_doc(),
				});
			},
		});
		set_primary_label(dialog);
		dialog.show();
	});
}

function set_primary_label(dialog) {
	if (!dialog) {
		return;
	}
	const kind = dialog.get_value("document_kind");
	dialog
		.get_primary_btn()
		.html(NOT_BOOKABLE_KINDS.includes(kind) ? __("Mark Not Bookable") : __("Save and Create Draft"));
}
