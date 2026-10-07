{{#if is_he}}
# {{ brand }}

{{ description }}

משחק סנייק שנבנה על ידי JARVIS.

## איך משחקים
- פותחים את `index.html` בדפדפן ולוחצים "התחלה".
- חיצים או WASD לתזוזה, רווח או P להשהיה, Enter להתחלה מחדש. במסך מגע מחליקים או משתמשים בכפתורים.
- כל פרי מוסיף נקודות; כל 50 נקודות עולים רמה והמשחק מאיץ. השיא נשמר בדפדפן.

## בדיקות
- `node --test` מריץ את בדיקות חוקי המשחק (`js/snake.js`).
{{else}}
# {{ brand }}

{{ description }}

A Snake game built by JARVIS.

## How to play
- Open `index.html` in a browser and press Start.
- Arrows or WASD to move, Space or P to pause, Enter to restart. On touch screens swipe or use the buttons.
- Every fruit scores; every 50 points the level goes up and the game speeds up. The high score is kept in the browser.

## Tests
- `node --test` runs the game-rule tests (`js/snake.js`).
{{/if}}
