import requests
import webbrowser
import http.server
from urllib.parse import urlparse, parse_qs

import json
import yaml

import atexit
import threading

# Integration imports
from integrations.ToDoIntegrations.Task import TaskItem

# MSAL authentication imports
from msal import PublicClientApplication
from msal import SerializableTokenCache
from requests_oauthlib import OAuth2Session
import os

# Kivy imports
from kivy.properties import ObjectProperty
from kivy.properties import StringProperty
from kivy.uix.boxlayout import BoxLayout

# The authorization code returned by Microsoft
# This needs to be global to allow the request handler to obtain it and pass it back to Aquire_Auth_Code()
authorization_response = None

# Request handler to parse url's get request and strip out authorization code as string. 
# Sets the global authorization_response variable to this value.
# Note that this class needs to be at the top of this file.
class RequestHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        global authorization_response
        query_components = parse_qs(urlparse(self.path).query)
        code = str(query_components['code'])
        authorization_response = code[2:len(code)-2]
        if self.path == '/':
            self.path = 'index.html'
        return http.server.SimpleHTTPRequestHandler.do_GET(self)

# This widget handles all transactions for the Microsoft To Do integration.
class ToDoWidget(BoxLayout):

    # Load the authentication_settings.yml file
    # Note: this file is not tracked by github, so it will need to be created before running
    stream = open('integrations/ToDoIntegrations/microsoft_authentication_settings.yml', 'r')
    settings = yaml.safe_load(stream)

    # The instance of the Public Client Application from MSAL. This is assigned in __init__
    app = None

    tasks = []

    # The settings required for msal to properly authenticate the user.
    msal = {
        'authority': "https://login.microsoftonline.com/common",
        'authorize_endpoint': "/oauth2/v2.0/authorize",
        'redirect_uri': "http://localhost:1080",
        'token_endpoint': "/oauth2/v2.0/token",
        'scopes': ["user.read", "Tasks.ReadWrite"],
        'headers': "",

        # The access token aquired in Aquire_Access_Token. This is a class variable for the cases
        # where there is an attempt to make a request again in the short time this token is valid for.
        # If that should happen, storing the token like this minimalizes the amount of requests needed
        # to Microsoft's servers
        'access_token': None
    }

    sign_in_label_text = StringProperty()
    sign_in_button = ObjectProperty()
    grid_layout = ObjectProperty()

    def __init__(self, **kwargs):
        # This is necessary because Azure does not guarantee
        # to return scopes in the same case and order as requested
        os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'
        os.environ['OAUTHLIB_IGNORE_SCOPE_CHANGE'] = '1'

        cache = self.Deserialize_Cache("integrations/ToDoIntegrations/microsoft_cache.bin")
        
        # Instantiate the Public Client App
        self.app = PublicClientApplication(self.settings['app_id'], authority=self.msal['authority'], token_cache=cache)

        # If an account exists in cache, get it now. If not, don't do anything and let user sign in on settings screen.
        if(self.app.get_accounts()):
            self.Render_Tasks_Threaded()

        super(ToDoWidget, self).__init__(**kwargs)

    #region MSAL

    # Create the cache object, deserialize it for use, and register it to be reserialized before the application quits.
    def Deserialize_Cache(self, cache_path):

        cache = SerializableTokenCache()
        if os.path.exists(cache_path):
            cache.deserialize(open(cache_path, "r").read())
            print("Reading MSAL token cache")

        # Register a function with atexit to make sure the cache is written to just before the application terminates.
        atexit.register(lambda:
            open(cache_path, "w").write(cache.serialize())
            # Hint: The following optional line persists only when state changed
            if cache.has_state_changed else None
        )

        return cache

    # Gets access token however it is needed and returns that token
    def Aquire_Access_Token(self):
        result = None
        accounts = self.app.get_accounts()

        if(self.msal['access_token'] == None):

            result = self.Pull_From_Token_Cache()

            if (result == None):
                # Then there was no token in the cache

                # Get auth code
                authCode = self.Aquire_Auth_Code(self.settings)

                # Aquire token from Microsoft with auth code and scopes from above
                result = self.app.acquire_token_by_authorization_code(authCode, scopes=self.msal["scopes"], redirect_uri=self.msal['redirect_uri'])
            
            # Strip down the result and convert it to a string to get the final access token
            self.msal['access_token'] = str(result['access_token'])
        
        if self.msal['access_token'] != None:
            return True
        else:
            print("Something went wrong and no token was obtained!")
            return False


    def Pull_From_Token_Cache(self):
        accounts = self.app.get_accounts()
        if accounts:
            # TODO: Will implement better account management later. For now, the first account found is chosen.
            return self.app.acquire_token_silent_with_error(self.msal["scopes"], account=accounts[0])
        else:
            print("No accounts were found.")
            return None

    # Aquire msal auth code from Microsoft
    def Aquire_Auth_Code(self, settings):

        # Use the global variable authorization_response instead of a local one
        global authorization_response

        # Begin localhost web server in a new thread to handle the get request that will come from Microsoft
        webServerThread = threading.Thread(target=self.Run_Localhost_Server)
        webServerThread.setDaemon(True)
        webServerThread.start()

        # Builds url from yml settings
        authorize_url = '{0}{1}'.format(self.msal['authority'], self.msal['authorize_endpoint'])

        # Begins OAuth session with app_id, scopes, and redirect_uri from yml
        aadAuth = OAuth2Session(settings['app_id'], scope=self.msal['scopes'], redirect_uri=self.msal['redirect_uri'])

        # Obtain final login url from the OAuth session
        sign_in_url, state = aadAuth.authorization_url(authorize_url)

        # Opens a web browser with the new sign in url
        webbrowser.open(sign_in_url, new=2, autoraise=True)

        # Waits until the web server thread closes before continuing
        # This ensures that an authorization response will be returned.
        webServerThread.join()

        # This function returns the global authorization_response when it is not equal to None
        return authorization_response
    
    #endregion

    # Run Get_Access_Code() in a new thread
    def Render_Tasks_Threaded(self):
        access_code_thread = threading.Thread(target=self.Render_Tasks)
        access_code_thread.setDaemon(True)
        access_code_thread.start()

    # Aquire a new access token or pull one from the cache. 
    # Assuming one was found, pull new task info from the API
    def Render_Tasks(self):
        success = self.Aquire_Access_Token()
        if success:
            self.sign_in_label_text = "You are signed in to Microsoft"
            self.sign_in_button.visible = False
            self.Aquire_Task_Info()

    # Aquire tasks from the API and render them on screen
    def Aquire_Task_Info(self):
        self.tasks = self.Get_Tasks()
        
        #This is how it should be able to work. Not sure why this doesn't work
        #grid_layout = self.ids['tasks_list']
        for task in self.tasks:

            # This is the checkbox item of the new task
            checkbox = task.children[1]
            checkbox.bind(active=self.Box_Checked)

            if not task in self.grid_layout.children:
                print("Adding new task:", task.title)
                self.grid_layout.add_widget(task)

    def Box_Checked(self, checkbox, value):
        print(checkbox, "checked with value", value)
        task = checkbox.parent
        old_status = task.Get_Status()

        if value:
            task.Mark_Complete()
            if old_status != task.Get_Status():
                self.Update_Task(task)
                self.Aquire_Task_Info()
        else:
            print("Task '", task.Get_Title(), "' is already commplete")
            task.Mark_Uncomplete()
            if old_status != task.Get_Status():
                self.Update_Task(task)

    # Gets To Do Task Lists from Microsoft's graph API
    # NOTE: This is usually only run by the Get_Tasks method, there should 
    # be no need to get task lists without pulling the tasks from them.
    def Get_Task_Lists(self):

        # Leave this empty to pull from all available task lists, or specify the names of task lists that you would like to pull from.
        lists_to_use = []

        to_return = []

        # Set up endpoint and headers for request
        # lists_endpoint = "https://graph.microsoft.com/beta/me/outlook/tasks"
        lists_endpoint = "https://graph.microsoft.com/v1.0/me/todo/lists"

        # Run the get request to the endpoint
        lists_response = requests.get(lists_endpoint,headers=self.msal['headers'])

        # If the request was a success, return the JSON data, else print an error code
        # TODO: replace print with thrown exception
        if(lists_response.status_code == 200):
            json_data = json.loads(lists_response.text)
            # This is a list of task lists available
            lists = json_data['value']

            if lists_to_use:
                for task_list in lists:
                    if lists_to_use.count(task_list['displayName']) > 0:
                        # Then this list is in my list of lists to use and I should be pulling data from it
                        to_return.append(task_list)
            else:
                to_return.extend(lists)
            return to_return
        else:
            print("The response did not return a success code. Returning nothing.")
            return None

    # Pulls individual tasks from the lists returned by Get_Task_Lists and returns them as a single list.
    def Get_Tasks(self):
        self.msal['headers'] = {'Content-Type':'application/json', 'Authorization':'Bearer {0}'.format(self.msal['access_token'])}
        task_lists = self.Get_Task_Lists()

        if not task_lists:
            print("There was an issue getting task lists.")
            return None

        tasks_endpoint_base = "https://graph.microsoft.com/v1.0/me/todo/lists/"

        all_tasks = []

        # Pull all tasks from the chosen lists and add them to the list of all_tasks
        for task_list in task_lists:
            endpoint = tasks_endpoint_base + task_list['id'] + "/tasks"

            while True:
                tasks_response = requests.get(endpoint, headers=self.msal['headers'])

                if tasks_response.status_code == 200:
                    json_data = json.loads(tasks_response.text)
                    json_value = json_data['value']
                    for task in json_value:
                        all_tasks.append(TaskItem(task, task_list['id']))

                if not '@odata.nextLink' in json_data:
                    break

                endpoint = json_data['@odata.nextLink']

        print("All Tasks:", [(test_task.Get_Title(), test_task.Get_Status()) for test_task in all_tasks])

        # Remove any completed tasks so that they are not added to this displayed list
        all_tasks[:] = [task for task in all_tasks if (task.Get_Status() != 'completed')]
        
        return all_tasks

    # Sends a patch request to Microsoft's graph API to update the task data for the specified task.
    def Update_Task(self, task):

        task_endpoint = "https://graph.microsoft.com/v1.0/me/todo/lists/" + task.Get_List_Id() + "/tasks/" + task.Get_Id()

        requests.patch(task_endpoint, data=task.Build_Json(), headers=self.msal['headers'])

    # Starts a basic web server on localhost port 1080 using 
    # the custom request handler defined at the start of this file.
    # This will only handle one request and then terminate
    def Run_Localhost_Server(self, server_class=http.server.HTTPServer, handler_class=RequestHandler):
        server_address = ('127.0.0.1', 1080)
        httpd = server_class(server_address, handler_class)
        httpd.handle_request()