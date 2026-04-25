const toggleText = document.getElementById("toggle-text");
const loginForm = document.getElementById("login-form");
const signupForm = document.getElementById("signup-form");
const formTitle = document.getElementById("form-title");
const errorMsg = document.getElementById("error-msg");

let isLogin = true;

// TOGGLE
toggleText.addEventListener("click", () => {
    isLogin = !isLogin;
    errorMsg.textContent = "";

    if (isLogin) {
        loginForm.style.display = "block";
        signupForm.style.display = "none";
        formTitle.textContent = "Login";
        toggleText.textContent = "Don't have an account? Sign up";
    } else {
        loginForm.style.display = "none";
        signupForm.style.display = "block";
        formTitle.textContent = "Sign Up";
        toggleText.textContent = "Already have an account? Login";
    }
});

// LOGIN
const loginButton = loginForm.querySelector("button");

loginButton.addEventListener("click", async () => {
    const inputs = loginForm.querySelectorAll("input");
    const username = inputs[0].value.trim();
    const password = inputs[1].value.trim();

    if (username === "" || password === "") {
        errorMsg.textContent = "Please fill all login fields";
        return;
    }

    try {
        const response = await fetch("/login", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                username: username,
                password: password
            })
        });

        const data = await response.json();

        if (!response.ok) {
            errorMsg.textContent = data.error;
        } else {
            errorMsg.textContent = data.message;
            if (data.redirect) {
                window.location.href = data.redirect;
            }
        }

    } catch (error) {
        errorMsg.textContent = "Server error";
    }
});

// SIGNUP
const signupButton = signupForm.querySelector("button");

signupButton.addEventListener("click", async () => {
    const inputs = signupForm.querySelectorAll("input");

    const username = inputs[0].value.trim();
    const password = inputs[1].value.trim();
    const confirmPassword = inputs[2].value.trim();

    if (username === "" || password === "" || confirmPassword === "") {
        errorMsg.textContent = "Please fill all signup fields";
        return;
    }

    if (password !== confirmPassword) {
        errorMsg.textContent = "Passwords do not match";
        return;
    }

    try {
        const response = await fetch("/signup", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                username: username,
                password: password
            })
        });

        const data = await response.json();

        if (!response.ok) {
            errorMsg.textContent = data.error;
        } else {
            errorMsg.textContent = data.message;
        }

    } catch (error) {
        errorMsg.textContent = "Server error";
    }
});