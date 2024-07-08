const puppeteer = require('puppeteer');

(async () => {
  const browser = await puppeteer.launch({
    headless: true, // Run in non-headless mode
    // slowMo: 1000, // Slow down operations
    args: ['--ignore-certificate-errors']
  });
  const page = await browser.newPage();

  // Ignore SSL errors
  await page.setExtraHTTPHeaders({
    'Accept-Language': 'en-US,en;q=0.9'
  });

  // Add error handling
  page.on('console', msg => console.log('PAGE LOG:', msg.text()));
  page.on('error', err => console.error('PAGE ERROR:', err));
  page.on('pageerror', pageErr => console.error('PAGE ERROR:', pageErr));

  await page.goto('http://localhost:8000', { waitUntil: 'networkidle2' });

  // Simulate the "Join" button click
  await page.click('#join');

  // Wait for the video element to load
  await page.waitForSelector('video');

  // Sleep for 1 second
  await new Promise(resolve => setTimeout(resolve, 2000)); // Wait for 10 seconds

  // Capture the screenshot
  const videoElement = await page.$('video');
  await videoElement.screenshot({ path: 'screenshot.png' });

  await browser.close();
})();
