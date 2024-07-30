const puppeteer = require('puppeteer');

(async () => {
  const browser = await puppeteer.launch({
    headless: true,
    args: ['--ignore-certificate-errors']
  });
  const page = await browser.newPage();

  await page.setExtraHTTPHeaders({
    'Accept-Language': 'en-US,en;q=0.9'
  });

  page.on('console', msg => console.log('PAGE LOG:', msg.text()));
  page.on('error', err => console.error('PAGE ERROR:', err));
  page.on('pageerror', pageErr => console.error('PAGE ERROR:', pageErr));

  await page.goto('http://localhost:8000', { waitUntil: 'networkidle2' });

  await page.click('#join');

  await page.waitForSelector('video');
  await new Promise(resolve => setTimeout(resolve, 2000));

  // Capture the screenshot of the video element
  const videoElement = await page.$('video');
  await videoElement.screenshot({ path: 'screenshot.png' });

  // Capture the screenshot of the Mapbox map element
  const mapElement = await page.$('#map');
  await mapElement.screenshot({ path: 'map.png' });

  await browser.close();
})();
