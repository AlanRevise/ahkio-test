import base64
import hashlib
import logging
import os
import tempfile
import zipfile
from datetime import datetime

import markupsafe
import requests
from lxml import etree
from odoo import _
from odoo import models
from odoo.exceptions import UserError
from odoo.tests.common import Form
from odoo.tools import DEFAULT_SERVER_DATE_FORMAT
from odoo.tools import float_repr

_logger = logging.getLogger(__name__)


class AccountEdiFormat(models.Model):
    _inherit = 'account.edi.format'

    def _needs_web_services(self):
        """ Indicate if the EDI must be generated asynchronously through to some web services.

        :return: True if such a web service is available, False otherwise.
        """
        self.ensure_one()
        if self.code != 'finvoice_3_0':
            return super()._needs_web_services()
        return True

    def _post_invoice_edi(self, invoices, test_mode=False):
        """ Create the file content representing the invoice (and calls web services if necessary).

        :param invoices:    A list of invoices to post.
        :param test_mode:   A flag indicating the EDI should only simulate the EDI without sending data.
        :returns:           A dictionary with the invoice as key and as value, another dictionary:
        * attachment:       The attachment representing the invoice in this edi_format if the edi was successfully posted.
        * error:            An error if the edi was not successfully posted.
        """
        self.ensure_one()
        if self.code != 'finvoice_3_0':
            return super()._post_invoice_edi(invoices, test_mode=test_mode)
        res = {}
        for invoice in invoices:
            try:
                attachment = self._export_finvoice(invoice)
                res[invoice] = {'success': True, 'attachment': attachment}
            except FinvoiceGenerationException as err:
                res[invoice] = {'error': err}
        return res

    def _export_finvoice(self, invoice):
        def format_date(dt):
            # Format the date in Finvoice-standard.
            dt = dt or datetime.now()
            return dt.strftime('%Y%m%d')

        def format_monetary(number, currency):
            # Format the monetary values to avoid trailing decimals (e.g. 90.85000000000001) and replace . with ,.
            return float_repr(number, currency.decimal_places).replace('.', ',')

        def vat_number_in_finnish_format(vat_number):
            beginning = vat_number[2:-1]
            end = vat_number[-1]
            return f'{beginning}-{end}'

        self.ensure_one()

        self._validate_required_fields(invoice)

        template_values = {
            'record': invoice,
            'format_date': format_date,
            'format_monetary': format_monetary,
            'invoice_line_values': [],
            'message_identifier': f"{datetime.now().strftime('%Y%m%d%H%M%S')}-{invoice.name}",
            'message_timestamp': datetime.now().astimezone().isoformat(),
            'vat_number_in_finnish_format': vat_number_in_finnish_format,
        }

        # Tax lines.
        aggregated_taxes_details = {line.tax_line_id.id: {
            'line': line,
            'tax_amount': -line.amount_currency if line.currency_id else -line.balance,
            'tax_base_amount': 0.0,
        } for line in invoice.line_ids.filtered('tax_line_id')}

        # Invoice lines.
        for i, line in enumerate(invoice.invoice_line_ids.filtered(lambda l: not l.display_type)):
            price_unit_with_discount = line.price_unit * (1 - (line.discount / 100.0))
            taxes_res = line.tax_ids.with_context(force_sign=line.move_id._get_tax_force_sign()).compute_all(
                price_unit_with_discount,
                currency=line.currency_id,
                quantity=line.quantity,
                product=line.product_id,
                partner=invoice.partner_id,
                is_refund=line.move_id.move_type in ('in_refund', 'out_refund'),
            )

            line_template_values = {
                'line': line,
                'index': i + 1,
                'tax_details': [],
                'net_price_subtotal': taxes_res['total_excluded'],
            }

            for tax_res in taxes_res['taxes']:
                tax = self.env['account.tax'].browse(tax_res['id'])
                line_template_values['tax_details'].append({
                    'tax': tax,
                    'tax_amount': tax_res['amount'],
                    'tax_base_amount': tax_res['base'],
                })

                if tax.id in aggregated_taxes_details:
                    aggregated_taxes_details[tax.id]['tax_base_amount'] += tax_res['base']

            template_values['invoice_line_values'].append(line_template_values)

        template_values['tax_details'] = list(aggregated_taxes_details.values())

        invoice_name_safe = invoice.name.replace('/', '_')

        pdf = self.env.ref('account.account_invoices')._render_qweb_pdf(invoice.id)[0]
        pdf_name = f"{invoice_name_safe}_finvoice.pdf"

        template_values['pdf_name'] = f'file://{pdf_name}'

        xml_content = markupsafe.Markup('<?xml version="1.0" encoding="UTF-8"?>')
        xml_content += self.env.ref('ahkio_finvoice.account_invoice_finvoice_export')._render(template_values)
        xml_name = f"{invoice_name_safe}_finvoice.xml"

        params = {
            'soft': 'Ahkio',
            'ver': '2.0',
            'TraID': invoice.company_id.ahkio_apix_transfer_id,
            't': datetime.now().strftime('%Y%m%d%H%M%S'),
        }

        digest_params = list(params.values())
        digest_params.append(invoice.company_id.ahkio_apix_transfer_key)

        digest = '+'.join(digest_params)
        digest = hashlib.sha256(digest.encode()).hexdigest()

        params['d'] = f'SHA-256:{digest}'

        with tempfile.NamedTemporaryFile(prefix=f'apix_invoice_{invoice_name_safe}_', suffix='.zip') as temp_zip_file:
            with zipfile.ZipFile(temp_zip_file, 'w') as zip:
                zip.writestr(xml_name, xml_content)
                zip.writestr(pdf_name, pdf)

            temp_zip_file.seek(0)

            url = 'https://test-api.apix.fi/invoices' if self._apix_env() == 'test' else 'https://api.apix.fi/invoices'

            response = requests.put(url, temp_zip_file.read(), params=params)

            _logger.info(f'Response from apix. Code: {response.status_code}. Body: {response.text}.')

            response_body_xml = etree.fromstring(response.text.encode())
            response_status = response_body_xml.xpath('/Response/Status')[0].text

            if response_status != 'OK':
                raise FinvoiceGenerationException(response.text)

            return self.env['ir.attachment'].create({
                'name': os.path.basename(temp_zip_file.name),
                'datas': base64.encodebytes(temp_zip_file.read()),
                'mimetype': 'application/zip'
            })

    def _create_invoice_from_xml_tree(self, filename, tree):
        """ Create a new invoice with the data inside the xml.

        :param filename: The name of the xml.
        :param tree:     The tree of the xml to import.
        :returns:        The created invoice.
        """
        # TO OVERRIDE
        self.ensure_one()
        if self.code != 'finvoice_3_0':
            return super()._create_invoice_from_xml_tree(filename, tree)
        return self._import_finvoice(tree, self.env['account.move'])

    def _import_finvoice(self, tree, invoice):
        # Recipient company
        elements = tree.xpath('//InvoiceRecipientOrganisationUnitNumber') or tree.xpath('//BuyerOrganisationUnitNumber')
        company = elements and self.env['res.company'].search(
            [('partner_id.ahkio_organisation_unit_number', '=', elements[0].text)], limit=1)
        if not company:
            elements = tree.xpath('//BuyerPartyIdentifier')
            company = elements and self.env['res.company'].search(
                [('company_registry', '=', elements[0].text)], limit=1
            )

        if not company:
            elements = tree.xpath('//BuyerOrganisationTaxCode')
            company = elements and self.env['res.company'].search([('vat', '=', elements[0].text)], limit=1)

        if company:
            invoice = invoice.with_context(company_id=company.id)
        else:
            company = self.env.company
            _logger.info("Company not found. The user's company is set by default.")

        if not self.env.is_superuser():
            if self.env.company != company:
                raise UserError(_("You can only import invoices to your company: %s", self.env.company.display_name))

        # Total amount.
        elements = tree.xpath('//InvoiceTotalVatIncludedAmount')
        total_amount = elements and float(elements[0].text.replace(',', '.')) or 0.0

        # Move type
        default_move_type = 'in_refund' if total_amount < 0 else 'in_invoice'

        with Form(invoice.with_context(default_move_type=default_move_type,
                                       account_predictive_bills_disable_prediction=True)) as invoice_form:
            # Partner (seller)
            partner = None
            elements = tree.xpath('//SellerOrganisationUnitNumber')
            if elements:
                partner = elements and self.env['res.partner'].search([('ahkio_organisation_unit_number', '=', elements[0].text)], limit=1)

            if not partner:
                elements = tree.xpath('//SellerPartyIdentifier')
                if elements:
                    partner = elements and self.env['res.partner'].search([('ahkio_company_registry', '=', elements[0].text)], limit=1)

            if not partner:
                elements = tree.xpath('//SellerOrganisationTaxCode')
                if elements:
                    partner = elements and self.env['res.partner'].search([('vat', '=', elements[0].text)], limit=1)

            if partner:
                invoice_form.partner_id = partner

            # Reference.
            elements = tree.xpath('//InvoiceNumber')
            if elements:
                invoice_form.ref = elements[0].text

            # Name.
            elements = tree.xpath('//EpiReference')
            if elements:
                invoice_form.payment_reference = elements[0].text

            # Comment.
            narration_elements = []
            elements = tree.xpath('//InvoiceTotalVatIncludedAmount')
            if elements:
                amount = elements[0].text
                currency_str = elements[0].get('AmountCurrencyIdentifier')
                amount = f'{amount} {currency_str}'
                narration_elements.append(_("Original amount from invoice") + ": " + amount)
            elements = tree.xpath('//SellerAccountID')
            if elements:
                if len(elements) == 1:
                    title = _("Account number from invoice")
                else:
                    title = _("Account numbers from invoice")
                account_details = title + ":"
                for el in elements:
                    account_details += "\n" + el.text
                narration_elements.append(account_details)
            elements = tree.xpath('//InvoiceFreeText')
            if elements:
                narration_elements.append(elements[0].text)
            narration_text = "\n\n".join(narration_elements)
            invoice_form.narration = narration_text

            # Total amount.
            elements = tree.xpath('//InvoiceTotalVatIncludedAmount')
            if elements:

                # Currency.
                if elements[0].attrib.get('AmountCurrencyIdentifier'):
                    currency_str = elements[0].attrib['AmountCurrencyIdentifier']
                    currency = self.env.ref('base.%s' % currency_str.upper(), raise_if_not_found=False)
                    if currency != self.env.company.currency_id and currency.active:
                        invoice_form.currency_id = currency

            # Date.
            elements = tree.xpath('//InvoiceDate')
            if elements:
                date_str = elements[0].text
                date_obj = datetime.strptime(date_str, '%Y%m%d')
                invoice_form.invoice_date = date_obj.strftime(DEFAULT_SERVER_DATE_FORMAT)

            # Due date.
            elements = tree.xpath('//InvoiceDueDate')
            if elements:
                date_str = elements[0].text
                date_obj = datetime.strptime(date_str, '%Y%m%d')
                invoice_form.invoice_date_due = date_obj.strftime(DEFAULT_SERVER_DATE_FORMAT)

            # Invoice lines.
            elements = tree.xpath('//InvoiceRow')
            if elements:
                for element in elements:
                    with invoice_form.invoice_line_ids.new() as invoice_line_form:

                        # Product.
                        line_elements = element.xpath('.//ArticleName')
                        if line_elements:
                            invoice_line_form.name = line_elements[0].text

                        # Quantity.
                        line_elements = element.xpath('.//DeliveredQuantity')
                        if line_elements:
                            invoice_line_form.quantity = float(line_elements[0].text.replace(',', '.'))

                        # Price Unit.
                        line_elements = element.xpath('.//UnitPriceAmount')
                        if line_elements:
                            invoice_line_form.price_unit = float(line_elements[0].text.replace(',', '.'))

                        # Discount.
                        line_elements = element.xpath('.//RowDiscountPercent')
                        if line_elements:
                            invoice_line_form.discount = float(line_elements[0].text.replace(',', '.'))

                        # Taxes
                        line_elements = element.xpath('.//RowVatRatePercent')
                        invoice_line_form.tax_ids.clear()
                        for tax_element in line_elements:
                            percentage = float(tax_element.text.replace(',', '.'))

                            tax = self.env['account.tax'].search([
                                ('company_id', '=', invoice_form.company_id.id),
                                ('amount_type', '=', 'percent'),
                                ('type_tax_use', '=', invoice_form.journal_id.type),
                                ('amount', '=', percentage),
                            ], limit=1)

                            if tax:
                                invoice_line_form.tax_ids.add(tax)

        return invoice_form.save()

    def _validate_required_fields(self, invoice):
        if not invoice.company_id.company_registry:
            raise FinvoiceGenerationException('Company registry (y-tunnus) missing from sender.')
        if not invoice.commercial_partner_id.ahkio_company_registry:
            raise FinvoiceGenerationException('Company registry (y-tunnus) missing from recipient.')
        if 'partner_shipping_id' in invoice._fields and invoice.partner_shipping_id and not invoice.partner_shipping_id.ahkio_company_registry:
            raise FinvoiceGenerationException('Company registry (y-tunnus) missing from shipping contact.')
        if not invoice.company_id.partner_id.bank_ids:
            raise FinvoiceGenerationException('No bank accounts configured for sender.')
        if not invoice.company_id.ahkio_apix_transfer_id:
            raise FinvoiceGenerationException('Apix transfer id missing from company config.')
        if not invoice.company_id.ahkio_apix_transfer_key:
            raise FinvoiceGenerationException('Apix transfer key missing from company config.')
        if not invoice.company_id.partner_id.ahkio_organisation_unit_number:
            raise FinvoiceGenerationException('Organisation unit number (OVT) missing from sender.')
        if not invoice.commercial_partner_id.ahkio_organisation_unit_number:
            raise FinvoiceGenerationException('Organisation unit number (OVT) missing from recipient.')
        if 'partner_shipping_id' in invoice._fields and invoice.partner_shipping_id and not invoice.partner_shipping_id.ahkio_organisation_unit_number:
            raise FinvoiceGenerationException('Organisation unit number (OVT) missing from shipping contact.')
        if not invoice.company_id.partner_id.ahkio_e_invoice_address:
            raise FinvoiceGenerationException('E-invoice address missing from sender.')
        if not invoice.commercial_partner_id.ahkio_e_invoice_address:
            raise FinvoiceGenerationException('E-invoice address missing from recipient.')
        if not invoice.company_id.partner_id.ahkio_e_invoice_intermediator:
            raise FinvoiceGenerationException('E-invoice intermediator missing from sender.')
        if not invoice.commercial_partner_id.ahkio_e_invoice_intermediator:
            raise FinvoiceGenerationException('E-invoice intermediator missing from recipient.')

    def _apix_env(self):
        return self.env['ir.config_parameter'].get_param('ahkio_finvoice.apix_env', 'production')


class FinvoiceGenerationException(Exception):
    pass
