document.addEventListener("DOMContentLoaded", () => {
  const box = document.querySelector('.weather-box');
  const sun = document.querySelector('.sun');
  const moon = document.querySelector('.moon');
  const starsContainer = document.querySelector('.stars');
  const grounds = document.querySelectorAll('.ground1, .ground2');

  // Generate Stars
  function createStars(count) {
    for (let i = 0; i < count; i++) {
      const star = document.createElement('div');
      star.classList.add('star');
      star.style.top = `${Math.random() * 100}%`;
      star.style.left = `${Math.random() * 100}%`;
      star.style.animationDelay = `${Math.random() * 2}s`;
      starsContainer.appendChild(star);
    }
  }

  createStars(20); // Create 20 stars

  /**
   * Converts a time string (e.g., "4:51:00pm") to milliseconds from midnight.
   * @param {string} timeString - The time string to convert.
   * @returns {number|null} - Milliseconds from midnight or null if invalid.
   */
function timeToMillisecondsFromMidnight(timeString) {
    if (typeof timeString !== 'string') {
        console.error(`Invalid input type for timeString: expected string, got ${typeof timeString}`);
        return null;
    }

    const timeFormat = /^(\d{1,2}):(\d{2}):(\d{2})\s?(am|pm)$/i;
    const match = timeString.match(timeFormat);
    if (!match) {
        console.error(`Invalid time string format: "${timeString}"`);
        return null;
    }

    let [_, hours, minutes, seconds, meridiem] = match;
    hours = parseInt(hours, 10);
    minutes = parseInt(minutes, 10);
    seconds = parseInt(seconds, 10);

    if (meridiem.toLowerCase() === 'pm' && hours !== 12) {
        hours += 12;
    }
    if (meridiem.toLowerCase() === 'am' && hours === 12) {
        hours = 0;
    }

    return hours * 3600000 + minutes * 60000 + seconds * 1000;
}


  // Define globalSunrise and globalSunset with valid time strings
  window.globalSunrise = "6:00:00am";  // Initial sunrise time
  window.globalSunset = "6:00:00pm";   // Initial sunset time

  // Cycle duration in milliseconds
  const totalCycleDuration = 24 * 60 * 60 * 1000;

  // Initialize variables
  let dawnStart, sunrise, sunset, dawnDuration, dawnEnd;
  let preDawnDuration, preDawnStart, dayStart, duskDuration, dayEnd;
  let duskEnd, night1Start, night1Duration, night2Start, night2End, halfDay;
  let dawnStart_a, dayStart_a, dayEnd_a, duskStart_a, duskEnd_a, night1Start_a, night2Start_a, preDawnStart_a;

  // Constants for time units
  const MILLISECONDS_IN_SECOND = 1000;
  const MILLISECONDS_IN_MINUTE = 60 * MILLISECONDS_IN_SECOND;
  const MILLISECONDS_IN_HOUR = 60 * MILLISECONDS_IN_MINUTE;

  // Define color phases (will be updated in updateVars)
  let colorPhases = [];

  /**
   * Updates all time-related variables based on sunrise and sunset.
   */
  function updateVars() {
      // Convert sunrise and sunset times to milliseconds from midnight
      sunrise = timeToMillisecondsFromMidnight(window.globalSunrise);
      sunset = timeToMillisecondsFromMidnight(window.globalSunset);

      if (sunrise === null || sunset === null) {
          console.error("Sunrise or sunset time conversion failed. Aborting updateVars.");
          return;
      }

      // In Implementation 1, dawnStart = sunrise = 6 AM
      dawnStart = sunrise;

      // Time durations in milliseconds
      dawnDuration = 0.5 * MILLISECONDS_IN_HOUR;     // 30 minutes for dawn
      duskDuration = 0.5 * MILLISECONDS_IN_HOUR;     // 30 minutes for dusk
      preDawnDuration = 1 * MILLISECONDS_IN_HOUR;   // 1 hour for pre-dawn
      night1Duration = 1 * MILLISECONDS_IN_HOUR;    // 1 hour for the first part of the night

      // Time calculations
      dawnEnd = dawnStart + dawnDuration;
      preDawnStart = (dawnStart - preDawnDuration + totalCycleDuration) % totalCycleDuration; // Prevent negative
      dayStart = dawnEnd;
      duskStart = sunset - duskDuration;
      dayEnd = duskStart - 0.2 * MILLISECONDS_IN_HOUR; // Subtract 12 minutes for transition
      duskEnd = duskStart + duskDuration;
      night1Start = duskEnd;
      night2Start = night1Start + night1Duration;

      // Calculate night2End based on the remaining time
      night2End = totalCycleDuration - (sunset - sunrise) - dawnDuration - duskDuration - night1Duration - preDawnDuration;

      // Calculate halfDay midpoint as a fraction of the cycle
      halfDay = ((sunset - sunrise) / 2 + sunrise) / totalCycleDuration;

      // Normalize to fractions of the cycle
      dawnStart_a = dawnStart / totalCycleDuration;
      dayStart_a = dayStart / totalCycleDuration;
      dayEnd_a = dayEnd / totalCycleDuration;
      duskStart_a = duskStart / totalCycleDuration;
      duskEnd_a = duskEnd / totalCycleDuration;
      night1Start_a = night1Start / totalCycleDuration;
      night2Start_a = night2Start / totalCycleDuration;
      preDawnStart_a = preDawnStart / totalCycleDuration;

      // Define colorPhases after calculating the fractions
      colorPhases = [
        { time: 0, top: top_color.night2, bottom: bottom_color.night2 },
        { time: preDawnStart_a, top: top_color.predawn, bottom: bottom_color.predawn },
        { time: dawnStart_a, top: top_color.dawn, bottom: bottom_color.dawn},
        { time: dayStart_a, top: top_color.day, bottom: bottom_color.day },
        { time: dayEnd_a, top: top_color.day, bottom: bottom_color.day },
        { time: duskStart_a, top: top_color.dusk, bottom: bottom_color.dusk },
        { time: night1Start_a, top: top_color.night1, bottom: bottom_color.night1 },
        { time: night2Start_a, top: top_color.night2, bottom: bottom_color.night2 },
        { time: 1, top: top_color.night2, bottom: bottom_color.night2 }
      ];

//      // Log the updated variables for debugging
//      console.log("Updated time variables:", {
//          dawnStart, dawnDuration, sunset, dawnEnd, preDawnStart, dayStart, duskStart,
//          dayEnd, duskEnd, night1Start, night2Start, night2End, halfDay, dawnStart_a,
//          dayStart_a, dayEnd_a, duskStart_a, duskEnd_a, night1Start_a, night2Start_a,
//          preDawnStart_a
//      });


  }

  const top_color = {
      night1: "#001d3d", // Dark blue
      night2: "#223344", // Medium gray-blue
      predawn: "#223344", // Medium gray-blue
      dawn: "#FFDAB9", // Peach
      day: "#87ceeb", // Sky blue
      dusk: "#FFA07A" // Light coral
  };

  const bottom_color = {
      night1: "#002a4a", // Slightly lighter than night1
      night2: "#1b2c3a", // Darker than night2
      predawn: "#445566", // Lighter than predawn
      dawn: "#FFEBCD", // Lighter peach
      day: "#B0E2FF", // Lighter sky blue
      dusk: "#FFB6A8" // Lighter coral
  };

  /**
   * Interpolates between two hex colors.
   * @param {string} color1 - First color in hex format (e.g., "#FF0000").
   * @param {string} color2 - Second color in hex format.
   * @param {number} factor - Interpolation factor between 0 and 1.
   * @returns {string} - Interpolated color in RGB format.
   */
  function interpolateColor(color1, color2, factor) {
    const c1 = parseInt(color1.slice(1), 16);
    const c2 = parseInt(color2.slice(1), 16);
    const r1 = (c1 >> 16) & 0xff, g1 = (c1 >> 8) & 0xff, b1 = c1 & 0xff;
    const r2 = (c2 >> 16) & 0xff, g2 = (c2 >> 8) & 0xff, b2 = c2 & 0xff;
    const r = Math.round(r1 + factor * (r2 - r1));
    const g = Math.round(g1 + factor * (g2 - g1));
    const b = Math.round(b1 + factor * (b2 - b1));
    return `rgb(${r}, ${g}, ${b})`;
  }

  /**
   * Calculates the position of the sun or moon based on the current time.
   * @param {number} currentTime - Current time as a fraction of the cycle (0 to 1).
   * @param {boolean} isSun - Whether to calculate for the sun (true) or moon (false).
   * @returns {Object|null} - {x: number, y: number} or null if not visible.
   */
  function calculatePosition(currentTime, isSun) {
    const boxHeight = box.clientHeight; // Total height of the box
    const boxWidth = box.clientWidth;   // Total width of the box
    const sunTop = boxHeight * 0.1;     // Sun's top position (10% height)
    const sunBottom = boxHeight;        // Sun's bottom position
    const moonTop = boxHeight * 0.2;    // Moon's peak (20% height)
    const moonBottom = boxHeight;       // Moon's starting/ending position

    const sunX = (boxWidth / 2) - (sun.clientWidth / 2); // Fixed X for sun
    const moonXCenter = boxWidth / 2;                    // Center X for moon

    if (isSun) {
      let y;

      // Sun movement logic
      if (currentTime >= dawnStart_a && currentTime < halfDay) {
        const factor = (currentTime - dawnStart_a) / (halfDay - dawnStart_a); // 0 to 1
        y = sunBottom * 0.85 - factor * (sunBottom - sunTop); // Move upward
      } else if (currentTime >= halfDay && currentTime < duskEnd_a) {
        const factor = (currentTime - halfDay) / (duskEnd_a - halfDay); // 0 to 1
        y = sunTop + factor * (sunBottom - sunTop); // Move downward
      } else {
        return null; // Sun not visible
      }

      return { x: sunX, y };
    } else {
      let x, y;

      // Moon movement logic with OR condition
      if (currentTime >= night1Start_a || currentTime < preDawnStart_a) {
        const factor = currentTime >= night1Start_a
          ? (currentTime - night1Start_a) / (1 - night1Start_a)
          : currentTime / preDawnStart_a; // 0 to 1

        const angle = Math.PI * factor; // Angle from 0 to π radians (half-circle)

        // Calculate X and Y positions based on sine and cosine
        x = moonXCenter + Math.cos(angle) * (boxWidth / 3); // X oscillates left to right
        y = moonBottom - Math.sin(angle) * (moonBottom - moonTop); // Y follows the arc (half-circle)

        return { x, y };
      } else {
        return null; // Moon not visible
      }
    }
  }

  /**
   * Returns the number of seconds since midnight.
   * @returns {number} - Seconds since midnight.
   */
  function getSecondsSinceMidnight() {
    const now = new Date();
    const hours = now.getHours();
    const minutes = now.getMinutes();
    const seconds = now.getSeconds();
    return hours * 3600 + minutes * 60 + seconds;
  }

  /**
   * Updates the scene by recalculating colors and positions based on the current time.
   */
  function updateScene() {
    updateVars();
    const elapsedTime = getSecondsSinceMidnight() * 1000;

    // Calculate currentTime based on elapsed time
    const currentTime = (elapsedTime % totalCycleDuration) / totalCycleDuration;

    // Find current and next phase
    let currentPhase, nextPhase;
    for (let i = 0; i < colorPhases.length - 1; i++) {
      if (currentTime >= colorPhases[i].time && currentTime < colorPhases[i + 1].time) {
        currentPhase = colorPhases[i];
        nextPhase = colorPhases[i + 1];
        break;
      }
    }
    if (!currentPhase || !nextPhase) {
      currentPhase = colorPhases[colorPhases.length - 1];
      nextPhase = colorPhases[0];
    }

    // Calculate interpolation factor
    let phaseDuration = nextPhase.time - currentPhase.time;
    if (phaseDuration <= 0) { // Handle phase wrap-around
      phaseDuration += 1;
    }
    let factor = (currentTime - currentPhase.time) / phaseDuration;
    if (factor < 0) { factor += 1; } // Adjust for wrap-around

    // Interpolated sky colors
    const skyTop = interpolateColor(currentPhase.top, nextPhase.top, factor);
    const skyBottom = interpolateColor(currentPhase.bottom, nextPhase.bottom, factor);
    box.style.background = `linear-gradient(to bottom, ${skyTop}, ${skyBottom})`;

    // Update grounds' colors based on phase
    let groundTopColor, groundBottomColor;
    let isNight = false;

    if (currentTime >= night1Start_a || currentTime < dawnStart_a) {
      isNight = true; // Nighttime
    } else {
      isNight = false; // Daytime
    }

    // Update stars visibility
    starsContainer.style.opacity = isNight ? 1 : 0;

    // Update moon visibility
    if (isNight) {
      moon.style.opacity = 1;
      moon.style.display = 'block';
    } else {
      moon.style.opacity = 0;
      moon.style.display = 'none';
    }

    if (currentTime >= night1Start_a || currentTime < dawnStart_a) {
      // Night
      groundTopColor = "#2f2b3c";
      groundBottomColor = "#091B21";
    } else if (currentTime >= dawnStart_a && currentTime < dayStart_a) {
      // Dawn Transitioning to Day
      const dawnFactor = (currentTime - dawnStart_a) / (dayStart_a - dawnStart_a);
      groundTopColor = interpolateColor("#2f2b3c", "#556B2F", dawnFactor);
      groundBottomColor = interpolateColor("#091B21", "#228B22", dawnFactor);
    } else if (currentTime >= dayStart_a && currentTime < halfDay) {
      // Day
      groundTopColor = "#556B2F";
      groundBottomColor = "#228B22";
    } else if (currentTime >= halfDay && currentTime < duskStart_a) {
      // Dusk Transitioning to Night
      const duskFactor = (currentTime - halfDay) / (duskStart_a - halfDay);
      groundTopColor = interpolateColor("#556B2F", "#2f2b3c", duskFactor);
      groundBottomColor = interpolateColor("#228B22", "#091B21", duskFactor);
    }

    // Apply ground gradients
    grounds.forEach(ground => {
      ground.style.background = `linear-gradient(${groundTopColor}, ${groundBottomColor})`;
    });

    // Calculate and Update Sun Position
    const sunPos = calculatePosition(currentTime, true);
    if (sunPos) {
      sun.style.transform = `translate(${sunPos.x}px, ${sunPos.y}px)`; // Position the sun
      sun.style.display = 'block';
      sun.style.opacity = 1;
    } else {
      sun.style.opacity = 0;
      sun.style.display = 'none';
    }

    // Calculate and Update Moon Position
    const moonPos = calculatePosition(currentTime, false);
    if (moonPos) {
      moon.style.transform = `translate(${moonPos.x}px, ${moonPos.y}px)`; // Position the moon
      moon.style.display = 'block';
    } else {
      moon.style.opacity = 0;
      moon.style.display = 'none';
    }

    // Schedule the next update
    requestAnimationFrame(updateScene);
  }

  // Initial update
  updateScene();

});
