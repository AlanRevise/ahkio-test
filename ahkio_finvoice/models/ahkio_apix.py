import hashlib
import io
import logging
import re
import zipfile
from datetime import datetime

import requests
from lxml import etree

from odoo import models, _

_logger = logging.getLogger(__name__)


class AhkioApix(models.Model):
    _auto = False
    _name = 'ahkio.apix'
    _description = 'Apix client for sending and receiving invoices'

    def list_invoice_zips(self, company):
        def extract_content_from_group(group):
            file = {}

            for value in group.xpath('./Value'):
                file[value.xpath('./@type')[0]] = value.xpath('./text()')[0]

            return file

        params = {
            'TraID': company.ahkio_apix_transfer_id,
            't': datetime.now().strftime('%Y%m%d%H%M%S'),
        }

        params['d'] = self._calculate_digest(params, company.ahkio_apix_transfer_key)

        url = 'https://test-terminal.apix.fi/list2' if self._apix_env() == 'test' else 'https://terminal.apix.fi/list2'

        response = requests.get(url, params=params)

        response_body_xml = etree.fromstring(response.text.encode())
        response_status = response_body_xml.xpath('/Response/Status')[0].text

        if response_status != 'OK':
            raise response.text

        files = response_body_xml.xpath('/Response/Content/Group')

        return list(map(extract_content_from_group, files))

    def download(self, file, storage_status='UNRECEIVED'):
        # Files with NEW storageStatus should never be fetched since they are not ready
        if file['StorageStatus'] == 'NEW':
            return self.env['account.move']

        if storage_status and file['StorageStatus'] != storage_status:
            return self.env['account.move']

        filename = f"apix_in_invoice_{file['StorageID']}"

        existing = self.env['ir.attachment'].sudo().search([
            ('name', '=', f'{filename}.zip'),
            ('res_model', '=', 'account.move'),
        ], limit=1)

        if existing:
            _logger.error(f'Apix attachment already exists for this invoice: {filename}.zip')
            return self.env['account.move']

        params = {
            'markreceived': 'yes',
            'SID': file['StorageID'],
            't': datetime.now().strftime('%Y%m%d%H%M%S'),
        }

        params['d'] = self._calculate_digest(params, file['StorageKey'])

        url = 'https://test-terminal.apix.fi/download' if self._apix_env() == 'test' else 'https://terminal.apix.fi/download'

        response = requests.get(url, params=params)

        if response.status_code != 200:
            _logger.error(f"Failed to download invoice {file['DocumentID']}")

        attachment = self.env['ir.attachment'].create({
            'name': f"{filename}.zip",
            'raw': response.content,
            'res_model': 'account.move',
            'type': 'binary',
        })

        with zipfile.ZipFile(io.BytesIO(response.content)) as zip_file:
            with zip_file.open(file['DocumentName']) as xml_doc:
                try:
                    tree = etree.fromstring(xml_doc.read())
                except Exception:
                    _logger.error(f"Could not parse finvoice xml for invoice {file['DocumentID']}. Invoice zip can be found as attachment {filename}.zip.")
                    return self.env['account.move']

            apix_attachments = tree.xpath('//InvoiceUrlText')

            attachment_files = {}

            for apix_attachment in apix_attachments:
                filename_uri = apix_attachment.text
                match = re.match(r'^file:\/\/(?P<filename>.+)$', filename_uri)

                if not match:
                    _logger.warning(f"Failed to parse attachment {filename_uri} from invoice {file['DocumentID']}")
                    continue

                filename = match.group('filename')

                try:
                    with zip_file.open(filename) as attachment_file:
                        attachment_files[filename] = attachment_file.read()
                except KeyError:
                    _logger.error(f'Invoice specified attachment {filename}, but it was not found in the Apix zip.')

        invoice = self.env.ref('ahkio_finvoice.edi_finvoice_3_0')._create_invoice_from_xml_tree(f'{filename}.xml', tree)

        attachment.res_id = invoice.id

        invoice_ctx = invoice.with_context(no_new_invoice=True, default_res_id=invoice.id)

        invoice_ctx.message_post(body=_("Apix invoice zip"), attachment_ids=[attachment.id])

        invoice_attachment_ids = []

        for attachment_filename, attachment_file in attachment_files.items():
            invoice_attachment = self.env['ir.attachment'].create({
                'name': attachment_filename,
                'raw': attachment_file,
                'res_model': 'account.move',
                'res_id': invoice.id,
                'type': 'binary',
            })

            invoice_attachment_ids.append(invoice_attachment.id)

        if invoice_attachment_ids:
            invoice_ctx.message_post(body=_("Invoice attachments"), attachment_ids=invoice_attachment_ids)

        self._cr.commit()

        return invoice

    def fetch_invoice_zips_for_company(self, company):
        files = self.list_invoice_zips(company)

        if files:
            _logger.info(f'Starting to fetch invoices for company {company.name} ({company.company_registry})')

        for file in files:
            if self.download(file):
                _logger.info(f"Downloaded invoice {file['DocumentID']} for company {company.name} ({company.company_registry})")

    def fetch_invoice_zips(self):
        _logger.info('Starting to fetch invoices from Apix')

        company_conditions = [
            ('ahkio_apix_transfer_id', '!=', None),
            ('ahkio_apix_transfer_id', '!=', ''),
            ('ahkio_apix_transfer_key', '!=', None),
            ('ahkio_apix_transfer_key', '!=', ''),
        ]

        for company in self.env['res.company'].search(company_conditions):
            self.fetch_invoice_zips_for_company(company)

    def _calculate_digest(self, params, key):
        digest_params = list(params.values())
        digest_params.append(key)

        digest = '+'.join(digest_params)

        return f'SHA-256:{hashlib.sha256(digest.encode()).hexdigest()}'

    def _apix_env(self):
        return self.env['ir.config_parameter'].get_param('ahkio_finvoice.apix_env', 'production')
