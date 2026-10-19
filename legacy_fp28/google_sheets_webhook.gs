// Fill these in when deploying the Apps Script — do NOT commit real values.
const SPREADSHEET_ID = 'YOUR_SPREADSHEET_ID';
const WEBHOOK_TOKEN = 'YOUR_WEBHOOK_TOKEN';

const TARGET_SHEET_NAMES = ['POLO', 'JACKET', 'HEADBAND'];

const SHEET_BY_PRODUCT_SLUG = {
  'single-shirt': 'POLO',
  'fresh-jacket': 'JACKET',
  'fresh-headband': 'HEADBAND',
};

const SHEET_BY_PRODUCT_CATEGORY = {
  single: 'POLO',
  polo: 'POLO',
  jacket: 'JACKET',
  headband: 'HEADBAND',
};

const HEADERS = [
  'orderId',
  'orderNumber',
  'roundNumber',
  'sequenceNumber',
  'status',
  'paymentStatus',
  'khantokTicket',
  'khantokTicketValue',
  'khantokTicketClaimedAt',
  'createdAt',
  'updatedAt',
  'lastEvent',
  'lastSyncedAt',
  'productSlug',
  'productName',
  'productShortName',
  'productTagline',
  'productCategory',
  'productImage',
  'unitPrice',
  'size',
  'quantity',
  'totalAmount',
  'studentCode',
  'email',
  'fullName',
  'phone',
  'school',
  'parentPhone',
  'slipOriginalName',
  'slipStoredName',
  'slipStoredPath',
  'slipMimeType',
  'slipSize',
  'slipUploadedAt',
];

function doPost(e) {
  try {
    const payload = JSON.parse((e.postData && e.postData.contents) || '{}');

    if (WEBHOOK_TOKEN) {
      const incomingToken = String(payload.token || '').trim();
      if (incomingToken !== WEBHOOK_TOKEN) {
        return jsonResponse({ ok: false, message: 'unauthorized' });
      }
    }

    const row = payload.row || {};
    const orderId = String(row.orderId || '').trim();
    if (!orderId) {
      return jsonResponse({ ok: false, message: 'orderId is required' });
    }

    const spreadsheet = SpreadsheetApp.openById(SPREADSHEET_ID);
    const targetSheetName = resolveTargetSheetName_(row);
    const sheet = getOrCreateSheet_(spreadsheet, targetSheetName);
    const values = HEADERS.map((header) => normalizeCellValue_(row[header]));

    const existingLocation = findOrderLocation_(spreadsheet, orderId);
    if (existingLocation && existingLocation.sheet.getName() !== targetSheetName) {
      existingLocation.sheet.deleteRow(existingLocation.rowIndex);
    }

    const existingRowIndex = findOrderRowIndex_(sheet, orderId);
    if (existingRowIndex > 1) {
      sheet.getRange(existingRowIndex, 1, 1, HEADERS.length).setValues([values]);
    } else {
      sheet.appendRow(values);
    }

    return jsonResponse({
      ok: true,
      orderId,
      sheetName: targetSheetName,
      event: payload.event || '',
    });
  } catch (error) {
    return jsonResponse({ ok: false, message: String(error) });
  }
}

function resetOrderSheets() {
  const spreadsheet = SpreadsheetApp.openById(SPREADSHEET_ID);
  TARGET_SHEET_NAMES.forEach((sheetName) => {
    const sheet = getOrCreateSheet_(spreadsheet, sheetName);
    sheet.clearContents();
    writeHeaders_(sheet);
  });

  return {
    ok: true,
    resetSheets: TARGET_SHEET_NAMES,
  };
}

function getOrCreateSheet_(spreadsheet, sheetName) {
  let sheet = spreadsheet.getSheetByName(sheetName);

  if (!sheet) {
    sheet = spreadsheet.insertSheet(sheetName);
  }

  ensureHeaders_(sheet);
  return sheet;
}

function ensureHeaders_(sheet) {
  if (sheet.getMaxColumns() < HEADERS.length) {
    sheet.insertColumnsAfter(sheet.getMaxColumns(), HEADERS.length - sheet.getMaxColumns());
  }

  if (sheet.getLastRow() === 0) {
    writeHeaders_(sheet);
    return;
  }

  const existingHeaders = sheet.getRange(1, 1, 1, HEADERS.length).getValues()[0];
  const hasHeaders = existingHeaders.some((value) => String(value || '').trim());
  if (!hasHeaders || String(existingHeaders[0] || '').trim() !== HEADERS[0]) {
    writeHeaders_(sheet);
  }
}

function writeHeaders_(sheet) {
  sheet.getRange(1, 1, 1, HEADERS.length).setValues([HEADERS]);
}

function resolveTargetSheetName_(row) {
  const productSlug = normalizeKey_(row.productSlug);
  if (SHEET_BY_PRODUCT_SLUG[productSlug]) {
    return SHEET_BY_PRODUCT_SLUG[productSlug];
  }

  const productCategory = normalizeKey_(row.productCategory);
  if (SHEET_BY_PRODUCT_CATEGORY[productCategory]) {
    return SHEET_BY_PRODUCT_CATEGORY[productCategory];
  }

  const productName = normalizeKey_(row.productName);
  if (productName.indexOf('bundle') !== -1) {
    return 'BUNDLE';
  }
  if (productName.indexOf('jacket') !== -1) {
    return 'JACKET';
  }
  if (productName.indexOf('headband') !== -1) {
    return 'HEADBAND';
  }

  return 'POLO';
}

function normalizeKey_(value) {
  return String(value || '').trim().toLowerCase();
}

function findOrderLocation_(spreadsheet, orderId) {
  for (let index = 0; index < TARGET_SHEET_NAMES.length; index += 1) {
    const sheet = spreadsheet.getSheetByName(TARGET_SHEET_NAMES[index]);
    if (!sheet) {
      continue;
    }

    const rowIndex = findOrderRowIndex_(sheet, orderId);
    if (rowIndex > 1) {
      return { sheet, rowIndex };
    }
  }

  return null;
}

function findOrderRowIndex_(sheet, orderId) {
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) {
    return -1;
  }

  const orderIds = sheet.getRange(2, 1, lastRow - 1, 1).getValues();
  for (let index = 0; index < orderIds.length; index += 1) {
    if (String(orderIds[index][0]).trim() === orderId) {
      return index + 2;
    }
  }

  return -1;
}

function normalizeCellValue_(value) {
  if (value === null || value === undefined) {
    return '';
  }
  if (typeof value === 'object') {
    return JSON.stringify(value);
  }
  return value;
}

function jsonResponse(payload) {
  const output = ContentService.createTextOutput(JSON.stringify(payload));
  output.setMimeType(ContentService.MimeType.JSON);
  return output;
}
